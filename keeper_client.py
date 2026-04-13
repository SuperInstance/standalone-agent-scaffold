"""
keeper_client.py — Keeper Agent Client for Pelagic Fleet.

Provides the ``KeeperClient`` class for secure communication with a Keeper
Agent. The Keeper is the fleet's centralised secret manager — agents never
store raw secrets locally. Instead, they register secrets with the Keeper
and receive opaque references that can be used for proxied API calls and
git operations.

Transport: HTTP/HTTPS with optional mTLS.
Auth: Agent token received during onboarding.

All outbound payloads are scrubbed for accidental secret leakage before
transmission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

# Re-use the scrubber from onboard module
from onboard import scrub_secrets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_KEEPER_TIMEOUT: int = 30  # seconds
_USER_AGENT: str = "PelagicAgent/0.1.0"


# ---------------------------------------------------------------------------
# KeeperClient
# ---------------------------------------------------------------------------

class KeeperClient:
    """Client library for communicating with a Pelagic Keeper Agent.

    The Keeper is responsible for:
    - Storing and managing secrets on behalf of agents
    - Proxying API calls so raw credentials never touch agent memory/disk
    - Proxying git push operations through authenticated channels
    - Maintaining an audit log of all secret usage

    Args:
        base_url: HTTP(S) URL of the Keeper Agent (e.g. ``http://localhost:8443``).
        agent_name: Logical name of this agent (used for audit trails).
        agent_token: Auth token received during onboarding. If *None*, the
                     client operates in unauthenticated mode (limited functionality).
        mtls_cert: Optional path to an mTLS client certificate (PEM).
        mtls_key:  Optional path to an mTLS private key (PEM).

    Example::

        client = KeeperClient(base_url="https://keeper.pelagic.local", agent_name="coder")
        client.register_agent(agent_id="pelagic/coder/abc123")
        ref = client.store_secret("openai_key", "sk-...")
        result = client.request_api_call("openai", "/v1/chat/completions", "POST", {})
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8443",
        agent_name: str = "unnamed-agent",
        agent_token: Optional[str] = None,
        mtls_cert: Optional[str] = None,
        mtls_key: Optional[str] = None,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.agent_name: str = agent_name
        self.agent_token: Optional[str] = agent_token
        self.mtls_cert: Optional[str] = mtls_cert
        self.mtls_key: Optional[str] = mtls_key
        self.logger = logging.getLogger(f"pelagic.keeper.{agent_name}")
        self._registered: bool = False
        self._public_key_hash: Optional[str] = None

    # ---- internal helpers -----------------------------------------------------

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Create an SSL context, optionally configuring mTLS."""
        if not self.base_url.startswith("https://"):
            return None

        ctx = ssl.create_default_context()
        if self.mtls_cert and self.mtls_key:
            ctx.load_cert_chain(certfile=self.mtls_cert, keyfile=self.mtls_key)
            self.logger.debug("mTLS enabled with cert=%s", self.mtls_cert)
        return ctx

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request against the Keeper Agent.

        All payloads are scrubbed for secret leakage before transmission.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            path: URL path relative to ``base_url``.
            body: Optional JSON-serialisable request body.
            headers: Additional HTTP headers.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            KeeperConnectionError: On network / HTTP errors.
            KeeperAuthError: On 401/403 responses.
        """
        url = f"{self.base_url}{path}"
        hdrs: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            **(headers or {}),
        }
        if self.agent_token:
            hdrs["Authorization"] = f"Bearer {self.agent_token}"

        data: Optional[bytes] = None
        if body is not None:
            raw = json.dumps(body)
            # Defence-in-depth: scrub before sending
            scrubbed = scrub_secrets(raw)
            data = scrubbed.encode("utf-8")
            # Double-check: if scrubber changed anything, log a warning
            if scrubbed != raw:
                self.logger.warning("Secret scrubber redacted data in request to %s %s", method, path)

        self.logger.debug("→ %s %s", method, url)

        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            ctx = self._build_ssl_context()
            with urllib.request.urlopen(req, timeout=DEFAULT_KEEPER_TIMEOUT, context=ctx) as resp:
                resp_body = resp.read().decode("utf-8")
                self.logger.debug("← %d %s", resp.status, url)
                return json.loads(resp_body) if resp_body else {}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise KeeperAuthError(f"Authentication failed: {exc.code} {exc.reason}") from exc
            raise KeeperConnectionError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise KeeperConnectionError(f"Connection error: {exc.reason}") from exc

    # ---- public API -----------------------------------------------------------

    def register_agent(self, agent_id: str, agent_public_key: Optional[str] = None) -> dict[str, Any]:
        """Register this agent with the Keeper.

        Args:
            agent_id: Fully qualified agent identifier (e.g. ``pelagic/coder/abc123``).
            agent_public_key: Optional public key for encrypted communication.

        Returns:
            Registration response with ``token_ref`` and ``agent_id``.

        Raises:
            KeeperConnectionError: If the Keeper is unreachable.
            KeeperAuthError: If registration is rejected.
        """
        payload: dict[str, Any] = {"agent_id": agent_id, "agent_name": self.agent_name}
        if agent_public_key:
            payload["public_key"] = agent_public_key
            self._public_key_hash = hashlib.sha256(agent_public_key.encode()).hexdigest()[:16]

        resp = self._request("POST", "/api/agents/register", body=payload)
        self._registered = True

        # Store the returned token for future requests
        token = resp.get("token")
        if token:
            self.agent_token = token

        self.logger.info("Agent %s registered with keeper (ref=%s)", agent_id, resp.get("token_ref", "N/A"))
        return resp

    def store_secret(self, secret_id: str, secret_value: str) -> dict[str, Any]:
        """Store a secret with the Keeper.

        The Keeper stores the secret securely and returns an opaque reference.
        The agent should **never** use the raw value after this point.

        Args:
            secret_id: Logical identifier for the secret (e.g. ``openai_api_key``).
            secret_value: The raw secret value (never logged or stored locally).

        Returns:
            Dict with ``ref`` (opaque reference) and ``secret_id``.
        """
        # Scrub the value from any logging
        self.logger.info("Storing secret: %s (value length=%d chars)", secret_id, len(secret_value))
        resp = self._request("POST", "/api/secrets/store", body={
            "secret_id": secret_id,
            "secret_value": secret_value,
            "agent_id": self.agent_name,
        })
        self.logger.info("Secret %s stored — ref=%s", secret_id, resp.get("ref", "N/A"))
        return resp

    def retrieve_secret(self, secret_id: str) -> dict[str, Any]:
        """Retrieve a secret **reference** (never the raw value).

        The Keeper validates that the requesting agent has access to this
        secret and returns a reference token that can be used for proxied
        API calls.

        Args:
            secret_id: Logical identifier of the secret.

        Returns:
            Dict with ``ref``, ``secret_id``, and ``status``.
        """
        resp = self._request("GET", f"/api/secrets/retrieve/{urllib.parse.quote(secret_id, safe='')}")
        self.logger.info("Retrieved ref for secret: %s", secret_id)
        return resp

    def request_api_call(
        self,
        service: str,
        endpoint: str,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Proxy an API call through the Keeper.

        The Keeper injects the appropriate credentials and forwards the
        request, ensuring raw secrets never reach the agent's memory or logs.

        Args:
            service: Target service name (e.g. ``openai``, ``github``).
            endpoint: API endpoint path (e.g. ``/v1/chat/completions``).
            method: HTTP method.
            headers: Additional headers to forward.
            body: Request body.

        Returns:
            Proxied API response.
        """
        payload: dict[str, Any] = {
            "service": service,
            "endpoint": endpoint,
            "method": method,
        }
        if headers:
            payload["headers"] = headers
        if body:
            payload["body"] = body

        resp = self._request("POST", "/api/proxy/call", body=payload)
        self.logger.info("Proxied %s %s → %s", method, endpoint, service)
        return resp

    def request_git_push(
        self,
        repo: str,
        branch: str,
        files: Optional[list[dict[str, str]]] = None,
        message: str = "auto commit from agent",
    ) -> dict[str, Any]:
        """Proxy a git push operation through the Keeper.

        The Keeper uses the stored git credentials to authenticate the push.

        Args:
            repo: Repository URL or path.
            branch: Target branch name.
            files: List of ``{path, content}`` dicts to commit.
            message: Commit message.

        Returns:
            Dict with push result and commit SHA.
        """
        resp = self._request("POST", "/api/proxy/git-push", body={
            "repo": repo,
            "branch": branch,
            "files": files or [],
            "message": message,
        })
        self.logger.info("Proxied git push to %s@%s", branch, repo)
        return resp

    def audit_log(self, limit: int = 50, secret_id: Optional[str] = None) -> dict[str, Any]:
        """Review the audit log of secret usage.

        Args:
            limit: Maximum number of entries to return.
            secret_id: Optional filter by secret identifier.

        Returns:
            Dict with ``entries`` list and pagination metadata.
        """
        params: list[str] = [f"limit={limit}"]
        if secret_id:
            params.append(f"secret_id={urllib.parse.quote(secret_id, safe='')}")
        query = f"/api/audit/log?{'&'.join(params)}"
        resp = self._request("GET", query)
        self.logger.info("Retrieved %d audit log entries", len(resp.get("entries", [])))
        return resp

    def health_check(self) -> dict[str, Any]:
        """Check Keeper Agent health.

        Returns:
            Dict with ``status``, ``version``, and ``uptime``.
        """
        return self._request("GET", "/health")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class KeeperConnectionError(Exception):
    """Raised when the Keeper Agent is unreachable or returns an HTTP error."""

    pass


class KeeperAuthError(KeeperConnectionError):
    """Raised when authentication with the Keeper fails (401/403)."""

    pass
