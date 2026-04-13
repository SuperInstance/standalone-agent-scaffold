"""
workshop.py — Workshop Manager for Pelagic Fleet Agents.

Manages the agent's local git workshop: a structured directory for recipes,
scripts, interpreters, bootcamp exercises, and dojo techniques. Provides
commit, snapshot, history, recipe building, and narrative generation
capabilities.

Workshop structure::

    workshop/
    ├── recipes/
    │   ├── hot/          # frequently used, optimized
    │   ├── med/          # occasional, decent performance
    │   └── cold/         # reference implementations
    ├── interpreters/     # custom language interpreters
    ├── scripts/          # automation scripts
    ├── bootcamp/         # skill training exercises
    └── dojo/             # advanced technique library
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPERINSTANCE_DIR: Path = Path.home() / ".superinstance"
DEFAULT_WORKSHOP_PATH: Path = SUPERINSTANCE_DIR / "workshop"

_RECIPE_TIERS = ["hot", "med", "cold"]

_WORKSHOP_DIRS = [
    "recipes/hot",
    "recipes/med",
    "recipes/cold",
    "interpreters",
    "scripts",
    "bootcamp",
    "dojo",
]


# ---------------------------------------------------------------------------
# WorkshopManager
# ---------------------------------------------------------------------------

class WorkshopManager:
    """Manages the agent's local git workshop.

    The workshop is the agent's persistent workspace — a git repository
    containing all artefacts produced during the agent's lifetime. It
    supports atomic commits, tagged snapshots, history browsing, recipe
    management, and narrative generation.

    Args:
        path: Root path for the workshop. Defaults to
              ``~/.superinstance/workshop``.

    Example::

        wm = WorkshopManager()
        wm.init_workshop()
        wm.recipe("hello.sh", "#!/bin/bash\\necho Hello", "bash", tier="hot")
        wm.commit("Add hello script")
        wm.snapshot("v0.1-initial")
        print(wm.narrative())
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.workshop_path: Path = Path(path) if path else DEFAULT_WORKSHOP_PATH
        self.logger = logging.getLogger("pelagic.workshop")
        self._git_available: Optional[bool] = None

    # ---- properties -----------------------------------------------------------

    @property
    def is_git_repo(self) -> bool:
        """Check if the workshop path is a valid git repository."""
        return (self.workshop_path / ".git").is_dir()

    @property
    def git_available(self) -> bool:
        """Check if the ``git`` CLI is available on the system."""
        if self._git_available is None:
            self._git_available = shutil.which("git") is not None
        return self._git_available

    # ---- initialisation -------------------------------------------------------

    def init_workshop(self, path: Optional[Path | str] = None) -> Path:
        """Create the workshop directory structure and initialise git.

        Idempotent — safe to call on an existing workshop.

        Args:
            path: Override the workshop root path for this call.

        Returns:
            Absolute path to the created workshop directory.

        Raises:
            RuntimeError: If git is required but not available.
        """
        root = Path(path) if path else self.workshop_path
        root.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        for subdir in _WORKSHOP_DIRS:
            (root / subdir).mkdir(parents=True, exist_ok=True)

        # Create .gitkeep files so empty dirs are tracked
        for subdir in _WORKSHOP_DIRS:
            gitkeep = root / subdir / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text("# This file ensures the directory is tracked by git.\n", encoding="utf-8")

        # Create workshop README
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Pelagic Agent Workshop\n\n"
                "This is the local workshop for a Pelagic fleet agent.\n\n"
                "## Structure\n\n"
                "- `recipes/` — Compiled commands and scripts (hot/med/cold tiers)\n"
                "- `interpreters/` — Custom language interpreters\n"
                "- `scripts/` — Automation scripts\n"
                "- `bootcamp/` — Skill training exercises\n"
                "- `dojo/` — Advanced technique library\n",
                encoding="utf-8",
            )

        # Initialise git repo
        if not self.is_git_repo and self.git_available:
            subprocess.run(
                ["git", "init", str(root)],
                check=True,
                capture_output=True,
            )
            self.logger.info("Git repository initialised at %s", root)

            # Initial commit if there are files
            try:
                subprocess.run(
                    ["git", "-C", str(root), "add", "-A"],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(root), "commit", "-m", "Workshop initialised"],
                    check=True,
                    capture_output=True,
                )
                self.logger.info("Initial workshop commit created.")
            except subprocess.CalledProcessError:
                pass  # Nothing to commit or already committed
        elif not self.git_available:
            self.logger.warning("git not available — workshop created without version control.")

        self.workshop_path = root
        self.logger.info("Workshop ready at %s", root)
        return root

    # ---- git operations -------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a git command in the workshop directory.

        Args:
            *args: Git command and arguments.

        Returns:
            Completed process result.

        Raises:
            RuntimeError: If git is not available.
            subprocess.CalledProcessError: If the git command fails.
        """
        if not self.git_available:
            raise RuntimeError("git is not available on this system")
        return subprocess.run(
            ["git", "-C", str(self.workshop_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def commit(self, message: str, files: Optional[list[str]] = None) -> dict[str, Any]:
        """Create an atomic git commit in the workshop.

        Args:
            message: Commit message (appended with agent metadata).
            files: Specific file paths to stage. If *None*, stages all changes.

        Returns:
            Dict with ``hash``, ``message``, and ``files_count``.

        Raises:
            RuntimeError: If the workshop is not a git repo or no changes exist.
        """
        if not self.is_git_repo:
            raise RuntimeError("Workshop is not a git repository — run init_workshop() first")

        # Stage files
        if files:
            for f in files:
                self._git("add", f)
        else:
            self._git("add", "-A")

        # Check if there's anything to commit
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            self.logger.debug("No changes to commit.")
            return {"hash": "", "message": message, "files_count": 0}

        # Create commit with structured message
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        full_message = f"{message}\n\n[Pelagic Agent — {timestamp}]"

        result = self._git("commit", "-m", full_message)
        commit_hash = result.stdout.strip()

        # Parse short hash from git log
        try:
            log = self._git("log", "-1", "--format=%h")
            short_hash = log.stdout.strip()
        except Exception:
            short_hash = commit_hash[:8] if commit_hash else "unknown"

        self.logger.info("Committed %s: %s", short_hash, message)
        return {"hash": short_hash, "message": message, "files_count": len(files) if files else 0}

    def snapshot(self, label: str) -> dict[str, Any]:
        """Create a tagged snapshot of the current workshop state.

        Args:
            label: Tag name for the snapshot (e.g. ``v0.1-initial``).

        Returns:
            Dict with ``tag`` and ``head`` hash.
        """
        if not self.is_git_repo:
            raise RuntimeError("Workshop is not a git repository")

        # Ensure we have a clean state — commit any pending changes
        try:
            self.commit(f"Snapshot: {label}")
        except RuntimeError:
            pass  # Nothing to commit

        # Create the tag
        tag_message = f"Snapshot {label} — {datetime.now(timezone.utc).isoformat()}"
        self._git("tag", "-a", label, "-m", tag_message)

        head = self._git("rev-parse", "HEAD").stdout.strip()[:8]
        self.logger.info("Snapshot '%s' created at %s", label, head)
        return {"tag": label, "head": head}

    def history(self, limit: int = 20, filter_str: Optional[str] = None) -> list[dict[str, Any]]:
        """Browse commit history with metadata.

        Args:
            limit: Maximum number of commits to return.
            filter_str: Optional substring filter for commit messages.

        Returns:
            List of commit dicts with ``hash``, ``message``, ``author``,
            ``date``, and ``subject``.
        """
        if not self.is_git_repo:
            return []

        # Use null-byte (%x00) as field separator and double-null as record separator
        fmt = "%H%x00%h%x00%s%x00%an%x00%aI%x00%x00"
        try:
            result = self._git("log", f"--max-count={limit}", f"--format={fmt}")
        except subprocess.CalledProcessError:
            return []

        raw = result.stdout
        entries: list[dict[str, Any]] = []

        # Split records on double-null
        for block in raw.split("\x00\x00"):
            block = block.strip()
            if not block:
                continue

            # Split fields on single-null
            parts = block.split("\x00")
            if len(parts) < 5:
                continue

            full_hash, short_hash, subject, author, date = [p.strip() for p in parts[:5]]
            message = subject

            if filter_str and filter_str.lower() not in message.lower():
                continue

            entries.append({
                "hash": full_hash[:8],
                "short_hash": short_hash,
                "message": message,
                "author": author,
                "date": date,
                "subject": subject,
            })

        return entries

    # ---- recipe management ----------------------------------------------------

    def recipe(
        self,
        name: str,
        content: str,
        language: str = "bash",
        tier: str = "med",
    ) -> dict[str, Any]:
        """Save a recipe/script to the workshop.

        Args:
            name: Recipe filename (e.g. ``deploy.sh``, ``analyze.py``).
            content: The recipe content / source code.
            language: Programming language (used for metadata).
            tier: Performance tier — ``hot``, ``med``, or ``cold``.

        Returns:
            Dict with ``path`` and ``tier``.

        Raises:
            ValueError: If tier is invalid.
        """
        if tier not in _RECIPE_TIERS:
            raise ValueError(f"Invalid tier '{tier}'. Must be one of: {_RECIPE_TIERS}")

        recipe_dir = self.workshop_path / "recipes" / tier
        recipe_dir.mkdir(parents=True, exist_ok=True)
        recipe_path = recipe_dir / name
        recipe_path.write_text(content, encoding="utf-8")

        # Write metadata alongside the recipe
        meta_path = recipe_dir / f".{name}.meta.json"
        meta = {
            "name": name,
            "language": language,
            "tier": tier,
            "created": datetime.now(timezone.utc).isoformat(),
            "size_bytes": len(content.encode("utf-8")),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        self.logger.info("Recipe saved: %s (tier=%s, lang=%s)", recipe_path, tier, language)
        return {"path": str(recipe_path), "tier": tier}

    def build_recipe(self, name: str, inputs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Execute a recipe by name.

        Searches all tiers (hot → med → cold) for the first matching recipe.

        Args:
            name: Recipe filename to execute.
            inputs: Optional input variables to pass to the recipe.

        Returns:
            Dict with ``exit_code``, ``stdout``, ``stderr``, and ``path``.
        """
        for tier in _RECIPE_TIERS:
            candidate = self.workshop_path / "recipes" / tier / name
            if candidate.exists():
                self.logger.info("Executing recipe: %s (tier=%s)", candidate, tier)

                if not os.access(candidate, os.X_OK):
                    # Try running with the interpreter
                    try:
                        result = subprocess.run(
                            [sys.executable, str(candidate)],
                            capture_output=True,
                            text=True,
                            timeout=60,
                            cwd=str(self.workshop_path),
                            env={**os.environ, **(inputs or {})},
                        )
                    except FileNotFoundError:
                        result = subprocess.run(
                            ["bash", str(candidate)],
                            capture_output=True,
                            text=True,
                            timeout=60,
                            cwd=str(self.workshop_path),
                        )
                else:
                    result = subprocess.run(
                        [str(candidate)],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        cwd=str(self.workshop_path),
                    )

                return {
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "path": str(candidate),
                    "tier": tier,
                }

        return {"exit_code": -1, "stdout": "", "stderr": f"Recipe not found: {name}", "path": ""}

    # ---- interpreter / compiler -----------------------------------------------

    def compile_custom(self, lang: str, source: str) -> dict[str, Any]:
        """Save a custom interpreter/compiler source to the workshop.

        Args:
            lang: Language identifier (e.g. ``mylang``).
            source: Source code of the interpreter/compiler.

        Returns:
            Dict with ``path`` and ``lang``.
        """
        interp_dir = self.workshop_path / "interpreters" / lang
        interp_dir.mkdir(parents=True, exist_ok=True)

        entry = interp_dir / f"{lang}.py"
        entry.write_text(source, encoding="utf-8")

        self.logger.info("Custom interpreter saved: %s", entry)
        return {"path": str(entry), "lang": lang}

    # ---- narrative ------------------------------------------------------------

    def narrative(self) -> str:
        """Generate a narrative of all commits — the 'story of work'.

        Walks through the commit history and produces a human-readable
        story describing the agent's work journey.

        Returns:
            A multi-line string narrative.
        """
        commits = self.history(limit=100)
        if not commits:
            return "This workshop is empty — no commits yet."

        lines: list[str] = [
            "The Story of This Workshop",
            "═" * 40,
            "",
        ]

        for i, c in enumerate(commits, 1):
            date = c.get("date", "unknown")
            msg = c.get("message", "<no message>")
            author = c.get("author", "unknown")
            h = c.get("hash", "?")[:8]
            lines.append(f"  {i}. [{date}] {msg}")
            lines.append(f"     by {author} ({h})")
            lines.append("")

        return "\n".join(lines)

    # ---- rewind ---------------------------------------------------------------

    def rewind(self, target: str) -> dict[str, Any]:
        """Checkout a historical state for inspection.

        Creates a temporary branch at the target ref so the agent can
        inspect past states without losing the current HEAD.

        Args:
            target: Git ref (commit hash, tag, or branch name).

        Returns:
            Dict with ``target``, ``branch``, and ``head``.
        """
        if not self.is_git_repo:
            raise RuntimeError("Workshop is not a git repository")

        branch_name = f"inspect-{target[:8]}"
        try:
            self._git("checkout", "-b", branch_name, target)
        except subprocess.CalledProcessError:
            # Branch might already exist — try to check it out
            try:
                self._git("checkout", branch_name)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"Cannot rewind to {target}: {exc}") from exc

        head = self._git("rev-parse", "HEAD").stdout.strip()[:8]
        self.logger.info("Rewound to %s on branch %s (head=%s)", target, branch_name, head)
        return {"target": target, "branch": branch_name, "head": head}
