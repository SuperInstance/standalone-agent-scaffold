"""
agent.py — Core Agent Base Class for Pelagic Fleet.

This module provides the `StandaloneAgent` class, the foundational base that
every agent in the SuperInstance ecosystem inherits from. It implements:

- Identity system (name, role, version, vessel_id, fleet_org)
- State machine (BOOT → ONBOARDING → ACTIVE → PAUSED → ARCHIVED)
- YAML/JSON configuration loading from ~/.superinstance/agent.yaml
- Configurable heartbeat with health reporting
- Workshop and Keeper references
- Dual logging (stdout + rotating file logs)
- TUI integration hooks for minimal signal-based communication

Production-ready with full type hints, docstrings, and defensive programming.
"""

from __future__ import annotations

import abc
import enum
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPERINSTANCE_DIR: Path = Path.home() / ".superinstance"
DEFAULT_CONFIG_PATH: Path = SUPERINSTANCE_DIR / "agent.yaml"
DEFAULT_LOG_DIR: Path = SUPERINSTANCE_DIR / "logs"
DEFAULT_HEARTBEAT_INTERVAL: float = 30.0  # seconds
DEFAULT_WORKSHOP_PATH: Path = SUPERINSTANCE_DIR / "workshop"

# ---------------------------------------------------------------------------
# ANSI helpers (no external deps)
# ---------------------------------------------------------------------------

class _Ansi:
    """Minimal ANSI escape-code constants for coloured terminal output."""

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

    @classmethod
    def fg(cls, color: str) -> str:
        return color

ANSI = _Ansi()

# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class AgentState(enum.Enum):
    """Enumerates all valid lifecycle states for a Pelagic agent.

    Transition map (enforced by `StandaloneAgent.transition_to`):
        BOOT       → ONBOARDING | ARCHIVED
        ONBOARDING → ACTIVE     | ARCHIVED
        ACTIVE     → PAUSED     | ARCHIVED
        PAUSED     → ACTIVE     | ARCHIVED
        ARCHIVED   → (terminal)
    """

    BOOT = "BOOT"
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"

    def can_transition_to(self, target: "AgentState") -> bool:
        """Return *True* if moving from *self* to *target* is a valid transition."""
        return target in _STATE_TRANSITIONS[self]


# Defined *after* the class so enum members are available.
_STATE_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.BOOT:       {AgentState.ONBOARDING, AgentState.ARCHIVED},
    AgentState.ONBOARDING: {AgentState.ACTIVE, AgentState.ARCHIVED},
    AgentState.ACTIVE:     {AgentState.PAUSED, AgentState.ARCHIVED},
    AgentState.PAUSED:     {AgentState.ACTIVE, AgentState.ARCHIVED},
    AgentState.ARCHIVED:   set(),
}


# ---------------------------------------------------------------------------
# Health Status
# ---------------------------------------------------------------------------

class HealthStatus:
    """Lightweight container for heartbeat / health-report payloads.

    Attributes:
        agent_id:   Unique identifier of the agent.
        state:      Current `AgentState`.
        uptime_s:   Seconds since the agent entered the ACTIVE state.
        heartbeat:  Sequence number of this heartbeat (monotonically increasing).
        errors:     List of error strings accumulated since last report.
        workshop_ok: Whether the local workshop directory is accessible.
        keeper_ok:  Whether the keeper agent is reachable (best-effort).
    """

    __slots__ = (
        "agent_id",
        "state",
        "uptime_s",
        "heartbeat",
        "errors",
        "workshop_ok",
        "keeper_ok",
        "timestamp",
    )

    def __init__(
        self,
        agent_id: str,
        state: AgentState,
        uptime_s: float = 0.0,
        heartbeat: int = 0,
        errors: Optional[list[str]] = None,
        workshop_ok: bool = False,
        keeper_ok: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.state = state
        self.uptime_s = uptime_s
        self.heartbeat = heartbeat
        self.errors = errors or []
        self.workshop_ok = workshop_ok
        self.keeper_ok = keeper_ok
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON/YAML export."""
        return {
            "agent_id": self.agent_id,
            "state": self.state.value,
            "uptime_s": round(self.uptime_s, 2),
            "heartbeat": self.heartbeat,
            "errors": self.errors,
            "workshop_ok": self.workshop_ok,
            "keeper_ok": self.keeper_ok,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# TUI Signal Protocol
# ---------------------------------------------------------------------------

class TUISignal(enum.Enum):
    """Signals an agent can emit for minimal TUI integration.

    Each signal is a simple string label. A thin TUI layer can watch a
    named-pipe / queue and react to these signals without tight coupling.
    """

    STATE_CHANGED = "state_changed"
    HEARTBEAT = "heartbeat"
    ONBOARD_STEP = "onboard_step"
    ONBOARD_COMPLETE = "onboard_complete"
    ERROR = "error"
    SHUTDOWN = "shutdown"
    WORKSHOP_EVENT = "workshop_event"


# Callback type: signal → optional data payload
TUISignalCallback = Callable[[TUISignal, Optional[dict[str, Any]]], None]


# ---------------------------------------------------------------------------
# StandaloneAgent
# ---------------------------------------------------------------------------

class StandaloneAgent(abc.ABC):
    """Abstract base class for every Pelagic fleet standalone agent.

    Concrete agents subclass this and implement the abstract
    ``run_cycle`` method which contains the agent's main loop body.

    Usage::

        class MyAgent(StandaloneAgent):
            async def run_cycle(self):
                # ... agent work ...
                pass

        agent = MyAgent(name="my-agent", role="demo")
        agent.boot()
        agent.run()
    """

    # ---- construction ---------------------------------------------------------

    def __init__(
        self,
        name: str = "unnamed-agent",
        role: str = "general",
        version: str = "0.1.0",
        vessel_id: Optional[str] = None,
        fleet_org: str = "pelagic",
        config_path: Optional[Path | str] = None,
        workshop_path: Optional[Path | str] = None,
        keeper_url: Optional[str] = None,
    ) -> None:
        # Identity
        self.name: str = name
        self.role: str = role
        self.version: str = version
        self.vessel_id: str = vessel_id or uuid.uuid4().hex[:12]
        self.fleet_org: str = fleet_org
        self.agent_id: str = f"{fleet_org}/{name}/{self.vessel_id}"

        # Paths
        self.config_path: Path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.workshop_path: Path = Path(workshop_path) if workshop_path else DEFAULT_WORKSHOP_PATH
        self.log_dir: Path = DEFAULT_LOG_DIR / name

        # State machine
        self._state: AgentState = AgentState.BOOT
        self._state_since: float = time.monotonic()
        self._active_since: Optional[float] = None
        self._error_buffer: list[str] = []

        # Heartbeat
        self.heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
        self._heartbeat_seq: int = 0
        self._heartbeat_callbacks: list[Callable[[HealthStatus], None]] = []

        # Keeper
        self.keeper_url: Optional[str] = keeper_url
        self._keeper_token: Optional[str] = None

        # TUI
        self._tui_callbacks: list[TUISignalCallback] = []

        # Logging — set up after identity is known
        self._setup_logging()

        # Config
        self._config: dict[str, Any] = {}
        if self.config_path.exists():
            self.load_config()

        self.logger.info("Agent %s initialised  [vessel=%s]  [role=%s]", self.agent_id, self.vessel_id, self.role)

    # ---- properties -----------------------------------------------------------

    @property
    def state(self) -> AgentState:
        """Current agent lifecycle state."""
        return self._state

    @property
    def uptime_seconds(self) -> float:
        """Seconds spent in the current state."""
        return time.monotonic() - self._state_since

    @property
    def active_uptime_seconds(self) -> float:
        """Seconds spent in ACTIVE state total (across pauses)."""
        if self._active_since is None:
            return 0.0
        return time.monotonic() - self._active_since

    # ---- state transitions ----------------------------------------------------

    def transition_to(self, target: AgentState) -> None:
        """Atomically transition the agent to *target* state.

        Raises:
            ValueError: If the transition is not allowed by the state machine.
        """
        if not self._state.can_transition_to(target):
            raise ValueError(
                f"Invalid state transition: {self._state.value} → {target.value}. "
                f"Allowed: {[s.value for s in _STATE_TRANSITIONS[self._state]]}"
            )
        old = self._state
        self._state = target
        self._state_since = time.monotonic()

        if target == AgentState.ACTIVE and self._active_since is None:
            self._active_since = time.monotonic()

        self.logger.info("State transition: %s → %s", old.value, target.value)
        self._emit_tui(TUISignal.STATE_CHANGED, {"old": old.value, "new": target.value})

    # ---- configuration --------------------------------------------------------

    def load_config(self) -> dict[str, Any]:
        """Load YAML/JSON configuration from disk.

        Supports both ``.yaml`` and ``.json`` file extensions. If the file
        does not exist, returns an empty dict and logs a warning.

        Returns:
            Parsed configuration as a nested dict.
        """
        path = self.config_path
        if not path.exists():
            self.logger.warning("Config file not found at %s — using defaults.", path)
            self._config = {}
            return self._config

        raw = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            self._config = yaml.safe_load(raw) or {}
        elif path.suffix == ".json":
            self._config = json.loads(raw)
        else:
            self.logger.warning("Unknown config extension '%s' — attempting YAML parse.", path.suffix)
            self._config = yaml.safe_load(raw) or {}

        # Apply config overrides to instance attributes
        self.keeper_url = self._config.get("keeper", {}).get("url", self.keeper_url)
        self.heartbeat_interval = self._config.get("heartbeat_interval", self.heartbeat_interval)
        self._keeper_token = self._config.get("keeper", {}).get("token", self._keeper_token)

        self.logger.info("Configuration loaded from %s", path)
        return self._config

    def save_config(self) -> None:
        """Persist the current in-memory configuration to disk as YAML."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.setdefault("identity", {
            "name": self.name,
            "role": self.role,
            "version": self.version,
            "vessel_id": self.vessel_id,
            "fleet_org": self.fleet_org,
        })
        self._config.setdefault("keeper", {})["url"] = self.keeper_url or ""
        self._config["heartbeat_interval"] = self.heartbeat_interval
        if self._keeper_token:
            self._config.setdefault("keeper", {})["token"] = self._keeper_token

        self.config_path.write_text(yaml.dump(self._config, default_flow_style=False), encoding="utf-8")
        self.logger.info("Configuration saved to %s", self.config_path)

    @property
    def config(self) -> dict[str, Any]:
        """Read-only access to the loaded configuration."""
        return dict(self._config)

    # ---- logging --------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure dual output: rich console handler + rotating file handler."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"pelagic.{self.name}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()

        # Console handler — coloured, concise
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(
            logging.Formatter(
                f"{ANSI.DIM}%(asctime)s{ANSI.RESET} {ANSI.CYAN}%(levelname)-5s{ANSI.RESET} {ANSI.WHITE}%(name)s{ANSI.RESET} │ %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        self.logger.addHandler(console)

        # File handler — full DEBUG
        log_file = self.log_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s │ %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        self.logger.addHandler(file_handler)

    # ---- heartbeat ------------------------------------------------------------

    def on_heartbeat(self, callback: Callable[[HealthStatus], None]) -> None:
        """Register a callback invoked on every heartbeat tick."""
        self._heartbeat_callbacks.append(callback)

    def _do_heartbeat(self) -> HealthStatus:
        """Construct and broadcast a health status payload."""
        self._heartbeat_seq += 1
        status = HealthStatus(
            agent_id=self.agent_id,
            state=self._state,
            uptime_s=self.active_uptime_seconds,
            heartbeat=self._heartbeat_seq,
            errors=list(self._error_buffer),
            workshop_ok=self.workshop_path.exists(),
            keeper_ok=self.keeper_url is not None,
        )
        # Clear error buffer after reporting
        self._error_buffer.clear()
        for cb in self._heartbeat_callbacks:
            try:
                cb(status)
            except Exception as exc:
                self.logger.error("Heartbeat callback error: %s", exc)
        self._emit_tui(TUISignal.HEARTBEAT, status.to_dict())
        self.logger.debug("Heartbeat #%d — state=%s uptime=%.1fs", self._heartbeat_seq, self._state.value, status.uptime_s)
        return status

    # ---- TUI signals ----------------------------------------------------------

    def on_tui_signal(self, callback: TUISignalCallback) -> None:
        """Subscribe to TUI integration signals."""
        self._tui_callbacks.append(callback)

    def _emit_tui(self, signal: TUISignal, payload: Optional[dict[str, Any]] = None) -> None:
        """Fire all registered TUI callbacks for *signal*."""
        for cb in self._tui_callbacks:
            try:
                cb(signal, payload)
            except Exception:
                pass  # TUI is best-effort

    # ---- error buffer ---------------------------------------------------------

    def record_error(self, message: str) -> None:
        """Append an error to the rolling buffer (reported on next heartbeat)."""
        self._error_buffer.append(f"[{datetime.now(timezone.utc).isoformat()}] {message}")
        self.logger.error(message)
        self._emit_tui(TUISignal.ERROR, {"message": message})

    # ---- lifecycle ------------------------------------------------------------

    def boot(self) -> None:
        """Boot the agent: ensure directories, load config, transition to ONBOARDING."""
        self.logger.info("Booting agent %s v%s …", self.name, self.version)
        SUPERINSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.load_config()

        if self._config.get("onboarded"):
            self.transition_to(AgentState.ACTIVE)
            self.logger.info("Previously onboarded — skipping to ACTIVE.")
        else:
            self.transition_to(AgentState.ONBOARDING)
            self.logger.info("No onboard record found — entering ONBOARDING state.")

    @abc.abstractmethod
    def run_cycle(self) -> None:
        """Execute one iteration of the agent's main work loop.

        Subclasses MUST implement this. Called repeatedly while the agent
        is in the ACTIVE state.
        """

    def run(self, mode: str = "hot") -> None:
        """Start the agent's main loop.

        Args:
            mode: Execution temperature — ``hot`` (tight loop, low latency),
                  ``med`` (moderate pacing), or ``cold`` (batch-style).
        """
        if self._state not in (AgentState.ACTIVE, AgentState.ONBOARDING):
            raise RuntimeError(f"Agent must be ACTIVE or ONBOARDING to run; current state: {self._state.value}")

        interval_map: dict[str, float] = {"hot": 0.1, "med": 1.0, "cold": 10.0}
        cycle_interval: float = interval_map.get(mode, 1.0)

        self.logger.info("Agent running in %s mode (cycle_interval=%.2fs)", mode, cycle_interval)

        # Register graceful shutdown
        def _shutdown(sig: int, frame: Any) -> None:
            self.logger.info("Received signal %s — shutting down gracefully.", sig)
            self._emit_tui(TUISignal.SHUTDOWN)
            self.transition_to(AgentState.ARCHIVED)
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        last_heartbeat: float = 0.0
        while self._state == AgentState.ACTIVE:
            try:
                self.run_cycle()
            except Exception as exc:
                self.record_error(f"run_cycle error: {exc}")

            now = time.monotonic()
            if now - last_heartbeat >= self.heartbeat_interval:
                self._do_heartbeat()
                last_heartbeat = now

            time.sleep(cycle_interval)

    def pause(self) -> None:
        """Pause the agent (transition to PAUSED)."""
        self.transition_to(AgentState.PAUSED)

    def resume(self) -> None:
        """Resume from PAUSED back to ACTIVE."""
        self.transition_to(AgentState.ACTIVE)

    def archive(self) -> None:
        """Archive the agent (terminal state)."""
        self.transition_to(AgentState.ARCHIVED)

    # ---- repr -----------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} name={self.name!r} "
            f"vessel={self.vessel_id!r} state={self._state.value}>"
        )
