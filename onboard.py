"""
onboard.py — Onboard Protocol Engine for Pelagic Fleet Agents.

Implements a multi-step, idempotent onboarding protocol that guides a new
agent through identity creation, keeper linking, secret registration,
GitHub configuration, fleet registration, bootcamp enrollment, and
verification.

State is persisted in ``~/.superinstance/onboard_state.json`` so every step
can be safely re-run without duplicating work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SUPERINSTANCE_DIR: Path = Path.home() / ".superinstance"
ONBOARD_STATE_PATH: Path = SUPERINSTANCE_DIR / "onboard_state.json"

# Regex patterns for secret detection (used to scrub data before it leaves)
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?key)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(token|secret|password|passwd|bearer)\s*[=:]\s*['\"]?[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(?i)github[_-]?token\s*[=:]\s*['\"]?(gh[ps]_[A-Za-z0-9_]{36,})"),
    re.compile(r"sk-[A-Za-z0-9]{48,}"),  # OpenAI-style keys
    re.compile(r"AKIA[A-Z0-9]{16}"),     # AWS access key IDs
    re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_onboard_state() -> dict[str, Any]:
    """Load the persisted onboard state, or return an empty dict."""
    if ONBOARD_STATE_PATH.exists():
        return json.loads(ONBOARD_STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_onboard_state(state: dict[str, Any]) -> None:
    """Persist onboard state to disk."""
    ONBOARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ONBOARD_STATE_PATH.write_text(
        json.dumps(state, indent=2, default=str), encoding="utf-8"
    )


def scrub_secrets(data: str) -> str:
    """Scan *data* for patterns that look like secrets and redact them.

    This is a **defence-in-depth** measure.  It does NOT replace proper
    secret management — it prevents accidental leakage in log messages,
    error payloads, and debug output.

    Args:
        data: Arbitrary text that may contain embedded secrets.

    Returns:
        The text with all detected secret patterns replaced with ``***REDACTED***``.
    """
    redacted = data
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("***REDACTED***", redacted)
    return redacted


def _generate_confirmation_code() -> str:
    """Generate a 6-character alphanumeric confirmation code."""
    return secrets.token_hex(3).upper()


# ---------------------------------------------------------------------------
# OnboardProtocol
# ---------------------------------------------------------------------------

class OnboardProtocol:
    """Multi-step onboarding protocol for Pelagic fleet agents.

    Each step is independent and idempotent — calling a step that has
    already been completed is a no-op.

    Steps:
        1. Identity          — name, role, version
        2. Keeper Link       — discover or configure keeper URL
        3. Secret Registration — collect keys/tokens, send to keeper
        4. GitHub Setup      — configure git identity, create workshop repo
        5. Fleet Registration — register with fleet index
        6. Bootcamp Enrollment — optionally enroll in skill bootcamp
        7. Verification      — test all connections, report status

    Attributes:
        logger: Standard Python logger for this module.
        state: Persisted onboard state dict.
    """

    def __init__(
        self,
        keeper_url: Optional[str] = None,
        skip_github: bool = False,
    ) -> None:
        self.logger = logging.getLogger("pelagic.onboard")
        self.state: dict[str, Any] = _load_onboard_state()
        self.keeper_url: Optional[str] = keeper_url or self.state.get("keeper_url")
        self.skip_github: bool = skip_github

    def _mark_step(self, step: str, result: dict[str, Any]) -> None:
        """Mark a step as completed and persist state."""
        self.state.setdefault("steps", {})[step] = {
            **result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_onboard_state(self.state)

    def _is_step_done(self, step: str) -> bool:
        """Check if a step has already been completed."""
        return step in self.state.get("steps", {})

    # ---- Step 1: Identity ----------------------------------------------------

    def step_identity(self) -> dict[str, Any]:
        """Step 1 — Establish agent identity.

        Prompts for agent name, role description, and version. If already
        completed, returns the stored identity without re-prompting.

        Returns:
            Dict with ``ok``, ``detail``, and stored identity fields.
        """
        if self._is_step_done("identity"):
            return {**self.state["steps"]["identity"], "ok": True}

        # Lazy import to avoid circular dependency when used standalone
        from cli import _prompt

        name = _prompt("Agent name", self.state.get("identity", {}).get("name", "my-agent"))
        role = _prompt("Role description", self.state.get("identity", {}).get("role", "general-purpose agent"))
        version = _prompt("Version", self.state.get("identity", {}).get("version", "0.1.0"))
        vessel_id = uuid.uuid4().hex[:12]

        identity = {
            "name": name,
            "role": role,
            "version": version,
            "vessel_id": vessel_id,
            "fleet_org": "pelagic",
            "agent_id": f"pelagic/{name}/{vessel_id}",
        }

        self.state["identity"] = identity
        self._mark_step("identity", {"detail": f"agent={name} vessel={vessel_id}"})
        self.logger.info("Identity established: %s", identity["agent_id"])

        return {"ok": True, "detail": f"agent={name} vessel={vessel_id}"}

    # ---- Step 2: Keeper Link -------------------------------------------------

    def step_keeper_link(self) -> dict[str, Any]:
        """Step 2 — Link to a Keeper Agent.

        Discovers or configures the keeper agent URL and verifies connectivity.

        Returns:
            Dict with ``ok``, ``detail``, and ``keeper_url``.
        """
        if self._is_step_done("keeper_link") and not self.keeper_url:
            self.keeper_url = self.state["steps"]["keeper_link"].get("keeper_url")

        if self._is_step_done("keeper_link"):
            return {**self.state["steps"]["keeper_link"], "ok": True, "keeper_url": self.keeper_url}

        from cli import _prompt

        url = self.keeper_url or _prompt("Keeper agent URL", "")
        if not url:
            return {"ok": False, "detail": "No keeper URL provided — skipping (can re-run later)"}

        # Best-effort connectivity check
        try:
            import urllib.request
            req = urllib.request.Request(f"{url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            self.logger.warning("Keeper at %s not reachable — saving URL for later.", url)

        self.keeper_url = url
        self.state["keeper_url"] = url
        self._mark_step("keeper_link", {"detail": f"url={url}", "keeper_url": url})
        return {"ok": True, "detail": f"keeper at {url}", "keeper_url": url}

    # ---- Step 3: Secret Registration -----------------------------------------

    def step_secret_registration(self) -> dict[str, Any]:
        """Step 3 — Register secrets with the Keeper.

        Collects API keys/tokens and sends them to the keeper agent.
        **Never stores secrets locally** — only receives back token references.

        Returns:
            Dict with ``ok``, ``detail``, and ``secrets_registered``.
        """
        if self._is_step_done("secret_registration"):
            return {**self.state["steps"]["secret_registration"], "ok": True}

        from cli import _prompt

        secrets_to_register: list[dict[str, str]] = []

        # Collect secrets
        api_key = _prompt("API Key (press Enter to skip)", "", secret=True)
        if api_key:
            secrets_to_register.append({"id": "api_key", "value": api_key})

        github_token = _prompt("GitHub Token (press Enter to skip)", "", secret=True)
        if github_token:
            secrets_to_register.append({"id": "github_token", "value": github_token})

        if not secrets_to_register:
            self._mark_step("secret_registration", {"detail": "no secrets to register"})
            return {"ok": True, "detail": "no secrets to register", "secrets_registered": 0}

        # Send to keeper (or simulate if no keeper available)
        registered_refs: list[str] = []
        if self.keeper_url:
            try:
                from keeper_client import KeeperClient
                client = KeeperClient(base_url=self.keeper_url)
                for sec in secrets_to_register:
                    result = client.store_secret(sec["id"], sec["value"])
                    registered_refs.append(result.get("ref", f"ref:{sec['id']}"))
            except Exception as exc:
                self.logger.error("Failed to register secrets with keeper: %s", exc)
                return {"ok": False, "detail": f"keeper error: {exc}"}
        else:
            # No keeper — generate confirmation code for manual registration
            code = _generate_confirmation_code()
            self.logger.warning(
                "No keeper available. Secrets NOT stored. Confirmation code: %s", code
            )
            # Scrub the secrets from any local output
            for sec in secrets_to_register:
                self.logger.info("Secret %s would be sent to keeper (code: %s)", sec["id"], code)
            registered_refs = [f"pending:{s['id']}" for s in secrets_to_register]

        # NEVER store raw secrets — only references
        self.state["secret_refs"] = registered_refs
        self._mark_step("secret_registration", {
            "detail": f"{len(registered_refs)} secret(s) registered",
            "secrets_registered": len(registered_refs),
        })

        return {"ok": True, "detail": f"{len(registered_refs)} secret(s)", "secrets_registered": len(registered_refs)}

    # ---- Step 4: GitHub Setup ------------------------------------------------

    def step_github_setup(self) -> dict[str, Any]:
        """Step 4 — Configure Git identity and create/clone workshop repo.

        Sets up the user's git config for the workshop and optionally
        creates a GitHub repository.

        Returns:
            Dict with ``ok``, ``detail``, and ``repo_path``.
        """
        if self.skip_github:
            return {"ok": True, "detail": "skipped (--skip-github)"}

        if self._is_step_done("github_setup"):
            return {**self.state["steps"]["github_setup"], "ok": True}

        from cli import _prompt

        git_name = _prompt("Git author name", self.state.get("github", {}).get("name", "Pelagic Agent"))
        git_email = _prompt("Git author email", self.state.get("github", {}).get("email", "agent@pelagic.ai"))

        # Configure git
        try:
            subprocess.run(["git", "config", "--global", "user.name", git_name], check=True, capture_output=True)
            subprocess.run(["git", "config", "--global", "user.email", git_email], check=True, capture_output=True)
        except FileNotFoundError:
            self.logger.warning("git not found — skipping git identity configuration.")
        except subprocess.CalledProcessError as exc:
            self.logger.error("git config failed: %s", exc.stderr.decode(errors="replace"))

        # Initialise workshop
        from workshop import WorkshopManager
        wm = WorkshopManager()
        repo_path = wm.init_workshop()

        self.state["github"] = {"name": git_name, "email": git_email}
        self._mark_step("github_setup", {"detail": f"workshop={repo_path}", "repo_path": str(repo_path)})
        return {"ok": True, "detail": f"workshop at {repo_path}", "repo_path": str(repo_path)}

    # ---- Step 5: Fleet Registration ------------------------------------------

    def step_fleet_registration(self) -> dict[str, Any]:
        """Step 5 — Register with the fleet index (oracle1-index).

        Returns:
            Dict with ``ok`` and ``detail``.
        """
        if self._is_step_done("fleet_registration"):
            return {**self.state["steps"]["fleet_registration"], "ok": True}

        identity = self.state.get("identity", {})
        agent_id = identity.get("agent_id", "unknown")

        # In production, this would POST to oracle1-index
        self.logger.info("Registering agent %s with fleet index …", agent_id)
        # Simulate fleet registration
        fleet_record = {
            "agent_id": agent_id,
            "role": identity.get("role", "unknown"),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
        }
        self.state["fleet"] = fleet_record
        self._mark_step("fleet_registration", {"detail": f"registered {agent_id}"})
        return {"ok": True, "detail": f"agent {agent_id} registered with fleet"}

    # ---- Step 6: Bootcamp Enrollment -----------------------------------------

    def step_bootcamp_enrollment(self) -> dict[str, Any]:
        """Step 6 — Optionally enroll in skill bootcamp.

        Returns:
            Dict with ``ok`` and ``detail``.
        """
        if self._is_step_done("bootcamp_enrollment"):
            return {**self.state["steps"]["bootcamp_enrollment"], "ok": True}

        from cli import _prompt

        enroll = _prompt("Enroll in bootcamp?", "yes")
        if enroll.lower().startswith("y"):
            self.state["bootcamp"] = {"enrolled": True, "enrolled_at": datetime.now(timezone.utc).isoformat()}
            self._mark_step("bootcamp_enrollment", {"detail": "enrolled"})
            return {"ok": True, "detail": "enrolled in bootcamp"}
        else:
            self.state["bootcamp"] = {"enrolled": False}
            self._mark_step("bootcamp_enrollment", {"detail": "skipped"})
            return {"ok": True, "detail": "bootcamp skipped"}

    # ---- Step 7: Verification ------------------------------------------------

    def step_verification(self) -> dict[str, Any]:
        """Step 7 — Test all connections and report status.

        Validates:
        - Workshop directory exists
        - Keeper is reachable (if configured)
        - Git identity is set
        - Onboard state is consistent

        Returns:
            Dict with ``ok``, ``detail``, and nested ``checks`` dict.
        """
        checks: dict[str, bool] = {}

        # Workshop check
        ws_path = SUPERINSTANCE_DIR / "workshop"
        checks["workshop"] = ws_path.exists()
        if not checks["workshop"]:
            self.logger.warning("Workshop directory missing: %s", ws_path)

        # Keeper check
        if self.keeper_url:
            try:
                import urllib.request
                req = urllib.request.Request(f"{self.keeper_url}/health", method="GET")
                with urllib.request.urlopen(req, timeout=5):
                    checks["keeper"] = True
            except Exception:
                checks["keeper"] = False
                self.logger.warning("Keeper not reachable: %s", self.keeper_url)
        else:
            checks["keeper"] = None  # N/A

        # Git identity check
        try:
            result = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
            checks["git"] = bool(result.stdout.strip())
        except FileNotFoundError:
            checks["git"] = False

        # Config check
        config_path = SUPERINSTANCE_DIR / "agent.yaml"
        checks["config"] = config_path.exists()

        # Mark onboard as complete
        all_ok = all(v is not False for v in checks.values())
        self.state["onboarded"] = True
        self.state["verified_at"] = datetime.now(timezone.utc).isoformat()
        _save_onboard_state(self.state)

        self._mark_step("verification", {"detail": f"checks={checks}"})

        # Also write the agent config with the onboarded flag
        config_path.parent.mkdir(parents=True, exist_ok=True)
        agent_config: dict[str, Any] = {}
        if config_path.exists():
            try:
                agent_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        agent_config["onboarded"] = True
        if self.keeper_url:
            agent_config.setdefault("keeper", {})["url"] = self.keeper_url
        agent_config.setdefault("identity", {}).update(self.state.get("identity", {}))
        config_path.write_text(yaml.dump(agent_config, default_flow_style=False), encoding="utf-8")

        detail_parts = [f"{k}={'OK' if v else ('N/A' if v is None else 'FAIL')}" for k, v in checks.items()]
        return {"ok": all_ok, "detail": ", ".join(detail_parts), "checks": checks}

    # ---- Run all steps --------------------------------------------------------

    def run_all(self) -> dict[str, Any]:
        """Execute all onboard steps in sequence.

        Returns:
            Summary dict with step-level results.
        """
        steps = [
            self.step_identity,
            self.step_keeper_link,
            self.step_secret_registration,
            self.step_github_setup,
            self.step_fleet_registration,
            self.step_bootcamp_enrollment,
            self.step_verification,
        ]

        results: dict[str, dict[str, Any]] = {}
        for step_fn in steps:
            step_name = step_fn.__name__.replace("step_", "")
            try:
                results[step_name] = step_fn()
            except Exception as exc:
                self.logger.error("Step %s failed: %s", step_name, exc)
                results[step_name] = {"ok": False, "detail": str(exc)}

        return results
