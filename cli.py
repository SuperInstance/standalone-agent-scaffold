"""
cli.py — Minimal TUI CLI Interface for Pelagic Fleet Agents.

Provides a beautiful terminal interface using ONLY Python stdlib (argparse + ANSI
escape codes). All subcommands delegate to the corresponding domain objects while
the CLI layer handles presentation, user interaction, and signal feedback.

Subcommands:
    onboard      — Run the interactive onboarding protocol
    run          — Start the agent in a given temperature mode
    status       — Display agent health, uptime, and workshop state
    config       — View or edit agent configuration
    workshop     — Open / manage the agent's local git workshop
    link-keeper  — Establish connection to a Keeper Agent
    audit        — Review commit history, trail logs, capability usage
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, NoReturn, Optional

# ---------------------------------------------------------------------------
# ANSI helpers — no external deps
# ---------------------------------------------------------------------------

class C:
    """ANSI colour constants and helper methods."""

    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    GREY    = "\033[90m"

    @staticmethod
    def style(text: str, *codes: str) -> str:
        """Wrap *text* in ANSI reset-delimited escape codes."""
        return f"{''.join(codes)}{text}{C.RESET}"


def _ok(label: str, detail: str = "") -> str:
    """Format a green OK status indicator."""
    suffix = f"  {C.DIM}{detail}{C.RESET}" if detail else ""
    return f"  {C.GREEN}● OK{C.RESET}  {C.BOLD}{label}{C.RESET}{suffix}"


def _warn(label: str, detail: str = "") -> str:
    """Format a yellow WARN status indicator."""
    suffix = f"  {C.DIM}{detail}{C.RESET}" if detail else ""
    return f"  {C.YELLOW}● WARN{C.RESET} {C.BOLD}{label}{C.RESET}{suffix}"


def _err(label: str, detail: str = "") -> str:
    """Format a red ERR status indicator."""
    suffix = f"  {C.DIM}{detail}{C.RESET}" if detail else ""
    return f"  {C.RED}● ERR{C.RESET}  {C.BOLD}{label}{C.RESET}{suffix}"


def _header(title: str, char: str = "─", width: int = 60) -> str:
    """Draw a section header."""
    return f"\n{C.CYAN}{C.BOLD}{title}{C.RESET}\n{char * width}"


def _prompt(prompt_text: str, default: str = "", secret: bool = False) -> str:
    """Read a line from stdin with an optional prompt and default.

    Args:
        prompt_text: Display text for the prompt.
        default: Default value shown in brackets.
        secret: If *True*, do not echo input (uses ``getpass`` when available).

    Returns:
        The trimmed user input, or *default* if the input is empty.
    """
    bracket = f" {C.DIM}[{default}]{C.RESET}" if default else ""
    full_prompt = f"{C.BOLD}{C.CYAN}→ {prompt_text}{bracket}: {C.RESET}"

    if secret:
        try:
            import getpass
            return getpass.getpass(full_prompt.replace(C.RESET, "").replace(C.BOLD, "").replace(C.CYAN, "")) or default
        except ImportError:
            pass

    try:
        return input(full_prompt) or default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


# ---------------------------------------------------------------------------
# Spinner (non-blocking, stdlib-only)
# ---------------------------------------------------------------------------

class Spinner:
    """A lightweight terminal spinner for long-running operations.

    Usage::

        with Spinner("Downloading fleet index …") as sp:
            fetch_index()
        # spinner auto-stops on context exit
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _INTERVAL = 0.08

    def __init__(self, message: str = "Working") -> None:
        self.message = message
        self._running = False

    def __enter__(self) -> "Spinner":
        self._running = True
        self._tick()
        return self

    def __exit__(self, *args: Any) -> None:
        self._running = False
        # Clear the spinner line
        print(f"\r{C.DIM}{' ' * 60}{C.RESET}\r", end="", flush=True)

    def _tick(self) -> None:
        if not self._running:
            return
        frame = self._FRAMES[int(time.monotonic() / self._INTERVAL) % len(self._FRAMES)]
        print(f"\r  {C.CYAN}{frame}{C.RESET} {self.message}", end="", flush=True)
        if self._running:
            import threading
            threading.Timer(self._INTERVAL, self._tick).start()

    def succeed(self, detail: str = "") -> None:
        """Replace the spinner with a green checkmark."""
        self._running = False
        suffix = f" {C.DIM}{detail}{C.RESET}" if detail else ""
        print(f"\r  {C.GREEN}✔{C.RESET}  {self.message}{suffix}")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_onboard(args: argparse.Namespace) -> int:
    """Execute the interactive onboarding protocol.

    Args:
        args: Parsed CLI arguments (expects ``--keeper-url``, ``--skip-github``).

    Returns:
        Exit code (0 = success, non-zero = failure).
    """
    from onboard import OnboardProtocol

    print(_header("🚀  Pelagic Agent — Onboarding Protocol"))

    proto = OnboardProtocol(
        keeper_url=getattr(args, "keeper_url", None),
        skip_github=getattr(args, "skip_github", False),
    )

    steps = [
        ("Identity",            proto.step_identity),
        ("Keeper Link",         proto.step_keeper_link),
        ("Secret Registration", proto.step_secret_registration),
        ("GitHub Setup",        proto.step_github_setup),
        ("Fleet Registration",  proto.step_fleet_registration),
        ("Bootcamp Enrollment", proto.step_bootcamp_enrollment),
        ("Verification",        proto.step_verification),
    ]

    for name, fn in steps:
        print(f"\n  {C.BOLD}{C.MAGENTA}Step: {name}{C.RESET}")
        try:
            with Spinner(f"Running {name} …") as sp:
                result = fn()
            if result.get("ok"):
                print(_ok(name, result.get("detail", "")))
            else:
                print(_warn(name, result.get("detail", "skipped")))
        except Exception as exc:
            print(_err(name, str(exc)))
            proto.logger.error("%s step failed: %s", name, exc)

    print(_header("Onboarding Complete", "═"))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Start the agent in the specified temperature mode.

    Args:
        args: Parsed CLI arguments (expects ``--mode``, ``--detach``).

    Returns:
        Exit code.
    """
    from agent import StandaloneAgent, AgentState

    class _CLIAgent(StandaloneAgent):
        def run_cycle(self) -> None:
            self.logger.debug("CLI agent cycle tick")

    agent = _CLIAgent()
    agent.boot()
    if agent.state != AgentState.ACTIVE:
        print(_err("Agent not active", "Run --onboard first to complete onboarding."))
        return 1

    mode = getattr(args, "mode", "hot")
    print(_ok(f"Agent running", f"mode={mode}  vessel={agent.vessel_id}"))
    agent.run(mode=mode)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Display current agent health and state information.

    Returns:
        Exit code (always 0 — status is informational).
    """
    from agent import StandaloneAgent, DEFAULT_CONFIG_PATH

    print(_header("📊  Agent Status"))

    agent = StandaloneAgent()
    agent.load_config()

    lines: list[str] = [
        f"  Agent ID   {C.BOLD}{agent.agent_id}{C.RESET}",
        f"  State      {C.BOLD}{agent.state.value}{C.RESET}",
        f"  Version    {C.DIM}{agent.version}{C.RESET}",
        f"  Config     {C.DIM}{agent.config_path}{C.RESET}",
        "",
    ]

    if agent.config_path.exists():
        lines.append(_ok("Config loaded", str(agent.config_path)))
    else:
        lines.append(_warn("No config found", "Run --onboard to create one."))

    if agent.workshop_path.exists():
        lines.append(_ok("Workshop", str(agent.workshop_path)))
    else:
        lines.append(_warn("Workshop missing", "Run 'workshop init' to create."))

    if agent.keeper_url:
        lines.append(_ok("Keeper linked", agent.keeper_url))
    else:
        lines.append(_warn("Keeper not linked", "Run 'link-keeper' to connect."))

    for line in lines:
        print(line)

    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """View or edit agent configuration.

    Returns:
        Exit code.
    """
    from agent import StandaloneAgent, DEFAULT_CONFIG_PATH

    agent = StandaloneAgent()
    agent.load_config()

    print(_header("⚙️  Agent Configuration"))

    if not agent.config:
        print(_warn("Empty configuration", "Run --onboard first."))
        return 1

    import json
    print(C.DIM + json.dumps(agent.config, indent=2, default=str) + C.RESET)
    print(f"\n  {C.DIM}Config path: {agent.config_path}{C.RESET}")
    return 0


def _cmd_workshop(args: argparse.Namespace) -> int:
    """Manage the agent's local git workshop.

    Returns:
        Exit code.
    """
    from workshop import WorkshopManager

    action = getattr(args, "workshop_action", "status")
    wm = WorkshopManager()

    print(_header("🔧  Workshop Manager"))

    if action == "init":
        path = wm.init_workshop()
        print(_ok("Workshop initialised", str(path)))
    elif action == "history":
        commits = wm.history(limit=getattr(args, "limit", 10))
        if not commits:
            print(_warn("No commits", "Workshop is empty."))
        else:
            for c in commits:
                print(f"  {C.DIM}{c.get('hash', '?')[:8]}{C.RESET}  {c.get('message', '<no message>')}")
    elif action == "narrative":
        story = wm.narrative()
        print(C.BOLD + "The Story of This Workshop" + C.RESET)
        print(C.DIM + story + C.RESET)
    else:
        if wm.workshop_path.exists():
            print(_ok("Workshop exists", str(wm.workshop_path)))
        else:
            print(_warn("Workshop not found", "Run 'workshop init' to create."))
    return 0


def _cmd_link_keeper(args: argparse.Namespace) -> int:
    """Establish connection to a Keeper Agent.

    Returns:
        Exit code.
    """
    from keeper_client import KeeperClient
    from agent import DEFAULT_CONFIG_PATH

    print(_header("🔐  Link Keeper Agent"))

    keeper_url = _prompt("Keeper URL", getattr(args, "keeper_url", ""))
    if not keeper_url:
        print(_err("No URL provided", "A keeper URL is required."))
        return 1

    agent_name = _prompt("Agent name", "unnamed-agent")

    client = KeeperClient(base_url=keeper_url, agent_name=agent_name)

    with Spinner("Testing connection …") as sp:
        try:
            reg = client.register_agent(agent_id=agent_name)
            sp.succeed(f"Connected to {keeper_url}")
            print(_ok("Keeper registered", f"token_ref={reg.get('token_ref', 'N/A')}"))

            # Persist keeper URL
            from agent import StandaloneAgent
            agent = StandaloneAgent(name=agent_name)
            agent.keeper_url = keeper_url
            agent._keeper_token = reg.get("token_ref")
            agent.save_config()
            print(_ok("Config saved", "Keeper link persisted."))
        except Exception as exc:
            sp.succeed()
            print(_err("Connection failed", str(exc)))
            return 1

    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """Review commit history, trail logs, and capability usage.

    Returns:
        Exit code.
    """
    from workshop import WorkshopManager

    print(_header("📋  Audit Trail"))

    wm = WorkshopManager()
    commits = wm.history(limit=getattr(args, "limit", 20))

    if not commits:
        print(_warn("No history", "Workshop has no commits to audit."))
        return 0

    print(f"  {C.BOLD}Recent Commits ({len(commits)}){C.RESET}")
    for i, c in enumerate(commits, 1):
        h = c.get("hash", "?")[:8]
        msg = c.get("message", "<no message>")
        date = c.get("date", "")
        author = c.get("author", "")
        print(f"  {C.DIM}{i:>3}.{C.RESET} {C.GREEN}{h}{C.RESET}  {msg}  {C.GREY}({author}, {date}){C.RESET}")

    print(f"\n  {C.DIM}Use 'workshop narrative' for a full story.{C.RESET}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser with all subcommands.

    Returns:
        Configured `ArgumentParser` instance.
    """
    parser = argparse.ArgumentParser(
        prog="pelagic-agent",
        description=C.style("Pelagic Fleet — Standalone Agent CLI", C.BOLD, C.CYAN),
        epilog=f"{C.DIM}Run 'pelagic-agent <command> --help' for command-specific options.{C.RESET}",
    )
    parser.add_argument("-V", "--version", action="version", version="%(prog)s 0.1.0")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # -- onboard ----------------------------------------------------------------
    p_onb = sub.add_parser("onboard", help="Run the interactive onboarding protocol")
    p_onb.add_argument("--keeper-url", default=None, help="Keeper agent URL")
    p_onb.add_argument("--skip-github", action="store_true", help="Skip GitHub setup step")

    # -- run --------------------------------------------------------------------
    p_run = sub.add_parser("run", help="Start the agent")
    p_run.add_argument("--mode", choices=["hot", "med", "cold"], default="hot", help="Execution temperature")
    p_run.add_argument("--detach", action="store_true", help="Run in background")

    # -- status -----------------------------------------------------------------
    sub.add_parser("status", help="Show agent health and state")

    # -- config -----------------------------------------------------------------
    sub.add_parser("config", help="View agent configuration")

    # -- workshop ---------------------------------------------------------------
    p_ws = sub.add_parser("workshop", help="Manage the local git workshop")
    p_ws.add_argument("workshop_action", nargs="?", default="status",
                       choices=["init", "status", "history", "narrative"], help="Workshop action")

    # -- link-keeper ------------------------------------------------------------
    p_lk = sub.add_parser("link-keeper", help="Link to a Keeper Agent")
    p_lk.add_argument("--keeper-url", default=None, help="Keeper agent URL")

    # -- audit ------------------------------------------------------------------
    p_audit = sub.add_parser("audit", help="Review commit history and logs")
    p_audit.add_argument("--limit", type=int, default=20, help="Number of entries to show")

    return parser


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Parse arguments and dispatch to the appropriate subcommand handler.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch: dict[str, Any] = {
        "onboard":     _cmd_onboard,
        "run":         _cmd_run,
        "status":      _cmd_status,
        "config":      _cmd_config,
        "workshop":    _cmd_workshop,
        "link-keeper": _cmd_link_keeper,
        "audit":       _cmd_audit,
    }

    if not args.command:
        parser.print_help()
        return 0

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Interrupted.{C.RESET}")
        return 130
    except Exception as exc:
        print(_err("Fatal", str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
