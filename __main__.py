"""
__main__.py — Entry Point for the Standalone Agent Scaffold.

Makes the package runnable via ``python -m standalone_agent_scaffold``.
Initialises logging, sets up signal handlers for graceful shutdown,
and delegates argument parsing to ``cli.main``.
"""

from __future__ import annotations

import logging
import os
import signal
import sys

# Ensure the package directory is on sys.path so sibling modules are importable
# regardless of how the user invokes the module.
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)


def _setup_root_logging() -> None:
    """Configure root logger with a clean, timestamped console format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _install_signal_handlers() -> None:
    """Register SIGINT/SIGTERM handlers for graceful shutdown."""
    logger = logging.getLogger("pelagic")

    def _handler(sig: int, frame: object) -> None:
        logger.info("Received signal %d — shutting down.", sig)
        sys.exit(128 + sig)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    """Application entry point.

    Sets up logging, installs signal handlers, and delegates to
    ``cli.main()`` for argument parsing and command dispatch.
    """
    _setup_root_logging()
    _install_signal_handlers()

    from cli import main as cli_main

    exit_code = cli_main()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
