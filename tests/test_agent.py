"""
tests/test_agent.py — Comprehensive tests for the Standalone Agent Scaffold.

Covers:
- Agent state machine transitions
- Configuration loading / saving
- Onboard protocol steps (with temp dirs and mocked IO)
- Keeper client (mocked HTTP transport)
- Workshop manager (real temp-dir git operations)
- CLI argument parsing
- Secret scrubbing utility

Run with::

    pytest tests/test_agent.py -v
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import yaml

# Ensure package modules are importable
import sys
_PACKAGE_DIR = str(Path(__file__).resolve().parent.parent)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

from agent import (
    AgentState,
    HealthStatus,
    StandaloneAgent,
    SUPERINSTANCE_DIR,
    TUISignal,
)
from onboard import OnboardProtocol, scrub_secrets, _SECRET_PATTERNS, _load_onboard_state, _save_onboard_state
from keeper_client import KeeperClient, KeeperConnectionError, KeeperAuthError
from workshop import WorkshopManager, _RECIPE_TIERS, DEFAULT_WORKSHOP_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ConcreteAgent(StandaloneAgent):
    """Minimal concrete subclass for testing abstract methods."""

    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        self._tmp_path = tmp_path
        super().__init__(
            config_path=tmp_path / "agent.yaml",
            workshop_path=tmp_path / "workshop",
            log_dir=tmp_path / "logs",
            **kwargs,
        )
        self.run_cycle_called = False

    def run_cycle(self) -> None:
        self.run_cycle_called = True


def _has_git() -> bool:
    """Check if git CLI is available."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ---------------------------------------------------------------------------
# Test: Agent State Machine
# ---------------------------------------------------------------------------

class TestAgentStateMachine(unittest.TestCase):
    """Validate the AgentState enum and its transition rules."""

    def test_all_states_exist(self) -> None:
        expected = {"BOOT", "ONBOARDING", "ACTIVE", "PAUSED", "ARCHIVED"}
        actual = {s.value for s in AgentState}
        self.assertEqual(actual, expected)

    def test_boot_to_onboarding(self) -> None:
        self.assertTrue(AgentState.BOOT.can_transition_to(AgentState.ONBOARDING))

    def test_boot_to_active_is_invalid(self) -> None:
        self.assertFalse(AgentState.BOOT.can_transition_to(AgentState.ACTIVE))

    def test_active_to_paused(self) -> None:
        self.assertTrue(AgentState.ACTIVE.can_transition_to(AgentState.PAUSED))

    def test_paused_to_active(self) -> None:
        self.assertTrue(AgentState.PAUSED.can_transition_to(AgentState.ACTIVE))

    def test_archived_is_terminal(self) -> None:
        self.assertEqual(AgentState.ARCHIVED._TRANSITIONS[AgentState.ARCHIVED], set())

    def test_all_transitions_valid(self) -> None:
        """Every target in a state's transition set should reciprocally allow."""
        for state, targets in AgentState._TRANSITIONS.items():
            for target in targets:
                # Not necessarily symmetric, but target should exist in _TRANSITIONS
                self.assertIn(target, AgentState._TRANSITIONS)


class TestStandaloneAgent(unittest.TestCase):
    """Test the StandaloneAgent base class lifecycle."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="pelagic_test_")
        self.tmp_path = Path(self.tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_agent_init_defaults(self) -> None:
        agent = _ConcreteAgent(self.tmp_path, name="test-agent")
        self.assertEqual(agent.name, "test-agent")
        self.assertEqual(agent.state, AgentState.BOOT)
        self.assertIsNotNone(agent.vessel_id)
        self.assertEqual(agent.fleet_org, "pelagic")
        self.assertTrue(agent.agent_id.startswith("pelagic/test-agent/"))

    def test_transition_boot_to_onboarding(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        agent.transition_to(AgentState.ONBOARDING)
        self.assertEqual(agent.state, AgentState.ONBOARDING)

    def test_transition_invalid_raises(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        with self.assertRaises(ValueError) as ctx:
            agent.transition_to(AgentState.ACTIVE)
        self.assertIn("Invalid state transition", str(ctx.exception))
        self.assertIn("BOOT", str(ctx.exception))

    def test_pause_resume(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        agent.transition_to(AgentState.ONBOARDING)
        agent.transition_to(AgentState.ACTIVE)
        agent.pause()
        self.assertEqual(agent.state, AgentState.PAUSED)
        agent.resume()
        self.assertEqual(agent.state, AgentState.ACTIVE)

    def test_archive_is_terminal(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        agent.transition_to(AgentState.ONBOARDING)
        agent.archive()
        self.assertEqual(agent.state, AgentState.ARCHIVED)
        with self.assertRaises(ValueError):
            agent.transition_to(AgentState.ACTIVE)

    def test_uptime_increases(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        t1 = agent.uptime_seconds
        time.sleep(0.05)
        t2 = agent.uptime_seconds
        self.assertGreater(t2, t1)

    def test_record_error(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        agent.record_error("something went wrong")
        # Error should be in the buffer
        health = agent._do_heartbeat()
        self.assertEqual(len(health.errors), 1)
        self.assertIn("something went wrong", health.errors[0])

    def test_heartbeat_increments(self) -> None:
        agent = _ConcreteAgent(self.tmp_path)
        agent.transition_to(AgentState.ONBOARDING)
        agent.transition_to(AgentState.ACTIVE)
        h1 = agent._do_heartbeat()
        h2 = agent._do_heartbeat()
        self.assertEqual(h2.heartbeat, h1.heartbeat + 1)

    def test_health_status_to_dict(self) -> None:
        hs = HealthStatus(
            agent_id="test/agent/123",
            state=AgentState.ACTIVE,
            uptime_s=42.0,
            heartbeat=5,
            errors=["err1"],
            workshop_ok=True,
            keeper_ok=False,
        )
        d = hs.to_dict()
        self.assertEqual(d["agent_id"], "test/agent/123")
        self.assertEqual(d["state"], "ACTIVE")
        self.assertEqual(d["uptime_s"], 42.0)
        self.assertEqual(d["errors"], ["err1"])
        self.assertTrue(d["workshop_ok"])
        self.assertFalse(d["keeper_ok"])
        self.assertIn("timestamp", d)


# ---------------------------------------------------------------------------
# Test: Configuration
# ---------------------------------------------------------------------------

class TestConfiguration(unittest.TestCase):
    """Test config loading and saving."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="pelagic_cfg_")
        self.tmp_path = Path(self.tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_missing_config(self) -> None:
        agent = _ConcreteAgent(self.tmp_path, name="test")
        config = agent.load_config()
        self.assertEqual(config, {})

    def test_load_yaml_config(self) -> None:
        cfg = self.tmp_path / "agent.yaml"
        cfg.write_text(
            yaml.dump({"keeper": {"url": "http://localhost:8443"}, "heartbeat_interval": 60}),
            encoding="utf-8",
        )
        agent = _ConcreteAgent(self.tmp_path, name="test", config_path=cfg)
        agent.load_config()
        self.assertEqual(agent.keeper_url, "http://localhost:8443")
        self.assertEqual(agent.heartbeat_interval, 60)

    def test_save_and_reload(self) -> None:
        cfg = self.tmp_path / "agent.yaml"
        agent = _ConcreteAgent(self.tmp_path, name="save-test", config_path=cfg)
        agent.keeper_url = "http://keeper:9999"
        agent.save_config()

        agent2 = _ConcreteAgent(self.tmp_path, name="save-test", config_path=cfg)
        agent2.load_config()
        self.assertEqual(agent2.keeper_url, "http://keeper:9999")

    def test_load_json_config(self) -> None:
        cfg = self.tmp_path / "agent.json"
        cfg.write_text(json.dumps({"heartbeat_interval": 15}), encoding="utf-8")
        agent = _ConcreteAgent(self.tmp_path, name="test", config_path=cfg)
        agent.load_config()
        self.assertEqual(agent.heartbeat_interval, 15)


# ---------------------------------------------------------------------------
# Test: Secret Scrubbing
# ---------------------------------------------------------------------------

class TestSecretScrubbing(unittest.TestCase):
    """Verify that the secret scrubber catches common leakage patterns."""

    def test_scrubs_api_key(self) -> None:
        data = "api_key = sk-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz567"
        result = scrub_secrets(data)
        self.assertNotIn("sk-abc123", result)
        self.assertIn("***REDACTED***", result)

    def test_scrubs_bearer_token(self) -> None:
        data = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.superlongtoken'
        result = scrub_secrets(data)
        self.assertIn("***REDACTED***", result)
        self.assertNotIn("eyJhbGci", result)

    def test_scrubs_aws_key(self) -> None:
        data = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = scrub_secrets(data)
        self.assertIn("***REDACTED***", result)

    def test_scrubs_private_key(self) -> None:
        data = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQ\n-----END RSA PRIVATE KEY-----"
        result = scrub_secrets(data)
        self.assertIn("***REDACTED***", result)

    def test_preserves_clean_text(self) -> None:
        data = "Hello, this is clean text with no secrets."
        result = scrub_secrets(data)
        self.assertEqual(result, data)

    def test_all_patterns_are_compiled(self) -> None:
        """All patterns in _SECRET_PATTERNS should be compiled regex objects."""
        for pat in _SECRET_PATTERNS:
            self.assertTrue(hasattr(pat, "match"), f"Pattern is not compiled: {pat}")


# ---------------------------------------------------------------------------
# Test: Onboard Protocol
# ---------------------------------------------------------------------------

class TestOnboardProtocol(unittest.TestCase):
    """Test the OnboardProtocol with mocked user input and temp directories."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="pelagic_onboard_")
        self.tmp_path = Path(self.tmpdir)

        # Patch SUPERINSTANCE_DIR to use temp dir
        self._patch_dir = patch("onboard.SUPERINSTANCE_DIR", self.tmp_path)
        self._patch_state = patch("onboard.ONBOARD_STATE_PATH", self.tmp_path / "onboard_state.json")
        self._patch_dir.start()
        self._patch_state.start()

    def tearDown(self) -> None:
        self._patch_dir.stop()
        self._patch_state.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("cli._prompt")
    def test_step_identity(self, mock_prompt: MagicMock) -> None:
        mock_prompt.side_effect = ["testy", "tester", "1.0.0"]
        proto = OnboardProtocol()
        result = proto.step_identity()
        self.assertTrue(result["ok"])
        self.assertEqual(proto.state["identity"]["name"], "testy")
        self.assertIn("vessel", result["detail"])

    @patch("cli._prompt")
    def test_step_identity_idempotent(self, mock_prompt: MagicMock) -> None:
        """Re-running a completed step should not re-prompt."""
        mock_prompt.side_effect = ["first", "first-role", "0.1.0"]
        proto = OnboardProtocol()
        proto.step_identity()
        # Reset mock — should not be called again
        mock_prompt.reset_mock()
        result = proto.step_identity()
        self.assertTrue(result["ok"])
        mock_prompt.assert_not_called()

    def test_step_keeper_link_with_url(self) -> None:
        proto = OnboardProtocol(keeper_url="http://localhost:9999")
        with patch("urllib.request.urlopen"):
            result = proto.step_keeper_link()
        self.assertTrue(result["ok"])
        self.assertEqual(proto.keeper_url, "http://localhost:9999")

    @patch("cli._prompt")
    def test_step_bootcamp_enroll(self, mock_prompt: MagicMock) -> None:
        mock_prompt.return_value = "yes"
        proto = OnboardProtocol()
        result = proto.step_bootcamp_enrollment()
        self.assertTrue(result["ok"])
        self.assertIn("enrolled", result["detail"])

    @patch("cli._prompt")
    def test_step_bootcamp_skip(self, mock_prompt: MagicMock) -> None:
        mock_prompt.return_value = "no"
        proto = OnboardProtocol()
        result = proto.step_bootcamp_enrollment()
        self.assertTrue(result["ok"])
        self.assertIn("skipped", result["detail"])

    @patch("cli._prompt")
    def test_step_fleet_registration(self, mock_prompt: MagicMock) -> None:
        mock_prompt.side_effect = ["fleet-test", "fleet role", "0.1.0"]
        proto = OnboardProtocol()
        proto.step_identity()
        result = proto.step_fleet_registration()
        self.assertTrue(result["ok"])
        self.assertIn("registered", result["detail"])


# ---------------------------------------------------------------------------
# Test: Keeper Client (Mocked HTTP)
# ---------------------------------------------------------------------------

class TestKeeperClient(unittest.TestCase):
    """Test the KeeperClient with mocked HTTP transport."""

    def test_init_defaults(self) -> None:
        client = KeeperClient(base_url="http://localhost:8443", agent_name="test")
        self.assertEqual(client.base_url, "http://localhost:8443")
        self.assertEqual(client.agent_name, "test")
        self.assertFalse(client._registered)

    def test_register_agent_success(self) -> None:
        client = KeeperClient(base_url="http://localhost:8443", agent_name="test")
        mock_response = json.dumps({"token_ref": "ref_123", "token": "tok_abc"}).encode()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = client.register_agent("pelagic/test/abc")
            self.assertTrue(client._registered)
            self.assertEqual(client.agent_token, "tok_abc")
            self.assertEqual(result["token_ref"], "ref_123")

    def test_store_secret(self) -> None:
        client = KeeperClient(base_url="http://localhost:8443", agent_name="test", agent_token="tok")
        mock_response = json.dumps({"ref": "sec_ref_1", "secret_id": "api_key"}).encode()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = client.store_secret("api_key", "sk-supersecret12345678901234567890123456")
            self.assertEqual(result["ref"], "sec_ref_1")

    def test_request_api_call(self) -> None:
        client = KeeperClient(base_url="http://localhost:8443", agent_name="test", agent_token="tok")
        mock_response = json.dumps({"status": "ok", "data": {"result": "hello"}}).encode()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = client.request_api_call("openai", "/v1/models", "GET")
            self.assertEqual(result["status"], "ok")

    def test_connection_error_raised(self) -> None:
        client = KeeperClient(base_url="http://nonexistent:9999", agent_name="test")

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            with self.assertRaises(KeeperConnectionError):
                client.health_check()

    def test_auth_error_raised(self) -> None:
        from urllib.error import HTTPError

        client = KeeperClient(base_url="http://localhost:8443", agent_name="test", agent_token="bad")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = HTTPError(
                url="http://localhost:8443/health",
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=None,
            )
            with self.assertRaises(KeeperAuthError):
                client.health_check()

    def test_secret_scrubbing_on_request(self) -> None:
        """Verify that secrets are scrubbed from outbound request payloads."""
        client = KeeperClient(base_url="http://localhost:8443", agent_name="test", agent_token="tok")

        captured_data: list[bytes] = []

        def capture_request(req: Any) -> None:
            if req.data:
                captured_data.append(req.data)

        mock_response = json.dumps({"ok": True}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            # Send a body that contains something that looks like a secret
            client._request("POST", "/test", body={"prompt": "my api_key=sk-abc123def456789012345678901234 is here"})

        # The captured data should have the secret redacted
        self.assertTrue(len(captured_data) > 0)
        self.assertNotIn(b"sk-abc123", captured_data[0])
        self.assertIn(b"REDACTED", captured_data[0])


# ---------------------------------------------------------------------------
# Test: Workshop Manager (real git in temp dirs)
# ---------------------------------------------------------------------------

@unittest.skipIf(not _has_git(), "git CLI not available")
class TestWorkshopManager(unittest.TestCase):
    """Test WorkshopManager with real git operations in temp directories."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="pelagic_ws_")
        self.tmp_path = Path(self.tmpdir)
        self.wm = WorkshopManager(path=self.tmp_path / "workshop")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_workshop(self) -> None:
        path = self.wm.init_workshop()
        self.assertTrue(path.exists())
        self.assertTrue((path / "recipes" / "hot").is_dir())
        self.assertTrue((path / "interpreters").is_dir())
        self.assertTrue((path / "dojo").is_dir())
        self.assertTrue(self.wm.is_git_repo)

    def test_init_workshop_idempotent(self) -> None:
        p1 = self.wm.init_workshop()
        p2 = self.wm.init_workshop()
        self.assertEqual(p1, p2)

    def test_commit(self) -> None:
        self.wm.init_workshop()
        # Create a file
        (self.wm.workshop_path / "test.txt").write_text("hello", encoding="utf-8")
        result = self.wm.commit("Add test file")
        self.assertNotEqual(result["hash"], "")

    def test_snapshot(self) -> None:
        self.wm.init_workshop()
        (self.wm.workshop_path / "test.txt").write_text("hello", encoding="utf-8")
        self.wm.commit("Add test file")
        result = self.wm.snapshot("v0.1")
        self.assertEqual(result["tag"], "v0.1")
        self.assertNotEqual(result["head"], "")

    def test_history(self) -> None:
        self.wm.init_workshop()
        (self.wm.workshop_path / "a.txt").write_text("a", encoding="utf-8")
        self.wm.commit("First commit")
        (self.wm.workshop_path / "b.txt").write_text("b", encoding="utf-8")
        self.wm.commit("Second commit")

        history = self.wm.history(limit=10)
        self.assertTrue(len(history) >= 2)
        messages = [h["message"] for h in history]
        self.assertIn("First commit", messages)
        self.assertIn("Second commit", messages)

    def test_history_with_filter(self) -> None:
        self.wm.init_workshop()
        (self.wm.workshop_path / "a.txt").write_text("a", encoding="utf-8")
        self.wm.commit("Add feature X")
        (self.wm.workshop_path / "b.txt").write_text("b", encoding="utf-8")
        self.wm.commit("Fix bug Y")

        filtered = self.wm.history(limit=10, filter_str="feature")
        self.assertEqual(len(filtered), 1)
        self.assertIn("feature", filtered[0]["message"])

    def test_recipe_save_and_build(self) -> None:
        self.wm.init_workshop()
        script = textwrap.dedent("""\
            #!/bin/bash
            echo "Hello from recipe"
        """).strip()
        result = self.wm.recipe("hello.sh", script, language="bash", tier="hot")
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["tier"], "hot")

    def test_recipe_invalid_tier(self) -> None:
        self.wm.init_workshop()
        with self.assertRaises(ValueError) as ctx:
            self.wm.recipe("x.sh", "# test", tier="invalid")
        self.assertIn("Invalid tier", str(ctx.exception))

    def test_compile_custom(self) -> None:
        self.wm.init_workshop()
        source = "# My custom interpreter\nprint('hello')"
        result = self.wm.compile_custom("mylang", source)
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["lang"], "mylang")

    def test_narrative(self) -> None:
        self.wm.init_workshop()
        (self.wm.workshop_path / "a.txt").write_text("a", encoding="utf-8")
        self.wm.commit("Create project")
        narrative = self.wm.narrative()
        self.assertIn("Create project", narrative)
        self.assertIn("Story", narrative)

    def test_rewind(self) -> None:
        self.wm.init_workshop()
        (self.wm.workshop_path / "a.txt").write_text("version 1", encoding="utf-8")
        self.wm.commit("V1")
        # Get the current head hash
        log = self.wm._git("rev-parse", "HEAD")
        v1_hash = log.stdout.strip()

        (self.wm.workshop_path / "a.txt").write_text("version 2", encoding="utf-8")
        self.wm.commit("V2")

        result = self.wm.rewind(v1_hash)
        self.assertEqual(result["target"], v1_hash)
        self.assertTrue(result["branch"].startswith("inspect-"))


# ---------------------------------------------------------------------------
# Test: Workshop without git
# ---------------------------------------------------------------------------

class TestWorkshopNoGit(unittest.TestCase):
    """Test WorkshopManager when git is not available."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="pelagic_wsnogit_")
        self.tmp_path = Path(self.tmpdir)
        self.wm = WorkshopManager(path=self.tmp_path / "workshop")
        # Force git unavailable
        self.wm._git_available = False

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_creates_dirs_without_git(self) -> None:
        path = self.wm.init_workshop()
        self.assertTrue(path.exists())
        self.assertTrue((path / "recipes" / "hot").is_dir())
        self.assertFalse(self.wm.is_git_repo)

    def test_commit_raises_without_git(self) -> None:
        self.wm.init_workshop()
        with self.assertRaises(RuntimeError):
            self.wm.commit("test")


# ---------------------------------------------------------------------------
# Test: CLI Argument Parsing
# ---------------------------------------------------------------------------

class TestCLIParsing(unittest.TestCase):
    """Test that the CLI argument parser handles all subcommands correctly."""

    def setUp(self) -> None:
        # Need cli module importable
        if _PACKAGE_DIR not in sys.path:
            sys.path.insert(0, _PACKAGE_DIR)

    def _parse(self, argv: list[str]) -> Any:
        from cli import build_parser
        return build_parser().parse_args(argv)

    def test_no_command(self) -> None:
        args = self._parse([])
        self.assertIsNone(args.command)

    def test_onboard(self) -> None:
        args = self._parse(["onboard"])
        self.assertEqual(args.command, "onboard")
        self.assertFalse(args.skip_github)

    def test_onboard_with_keeper(self) -> None:
        args = self._parse(["onboard", "--keeper-url", "http://k:8443"])
        self.assertEqual(args.keeper_url, "http://k:8443")

    def test_run_default_mode(self) -> None:
        args = self._parse(["run"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.mode, "hot")

    def test_run_cold_mode(self) -> None:
        args = self._parse(["run", "--mode", "cold"])
        self.assertEqual(args.mode, "cold")

    def test_status(self) -> None:
        args = self._parse(["status"])
        self.assertEqual(args.command, "status")

    def test_config(self) -> None:
        args = self._parse(["config"])
        self.assertEqual(args.command, "config")

    def test_workshop_init(self) -> None:
        args = self._parse(["workshop", "init"])
        self.assertEqual(args.workshop_action, "init")

    def test_workshop_history(self) -> None:
        args = self._parse(["workshop", "history"])
        self.assertEqual(args.workshop_action, "history")

    def test_workshop_default_status(self) -> None:
        args = self._parse(["workshop"])
        self.assertEqual(args.workshop_action, "status")

    def test_link_keeper(self) -> None:
        args = self._parse(["link-keeper", "--keeper-url", "http://k:9"])
        self.assertEqual(args.command, "link-keeper")
        self.assertEqual(args.keeper_url, "http://k:9")

    def test_audit_with_limit(self) -> None:
        args = self._parse(["audit", "--limit", "5"])
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.limit, 5)


# ---------------------------------------------------------------------------
# Test: TUI Signals
# ---------------------------------------------------------------------------

class TestTUISignals(unittest.TestCase):
    """Test the TUISignal enum and callback mechanism."""

    def test_all_signals_exist(self) -> None:
        expected = {
            "STATE_CHANGED", "HEARTBEAT", "ONBOARD_STEP",
            "ONBOARD_COMPLETE", "ERROR", "SHUTDOWN", "WORKSHOP_EVENT",
        }
        actual = {s.value for s in TUISignal}
        self.assertEqual(actual, expected)

    def test_tui_callback_receives_signal(self) -> None:
        agent = _ConcreteAgent(Path(tempfile.mkdtemp()))
        received: list[tuple[TUISignal, Any]] = []

        def cb(signal: TUISignal, payload: Any) -> None:
            received.append((signal, payload))

        agent.on_tui_signal(cb)
        agent.transition_to(AgentState.ONBOARDING)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], TUISignal.STATE_CHANGED)
        self.assertIn("old", received[0][1])
        self.assertIn("new", received[0][1])


# ---------------------------------------------------------------------------
# Test: Onboard State Persistence
# ---------------------------------------------------------------------------

class TestOnboardStatePersistence(unittest.TestCase):
    """Test that onboard state can be saved and loaded correctly."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="pelagic_state_")
        self.state_path = Path(self.tmpdir) / "state.json"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load(self) -> None:
        with patch("onboard.ONBOARD_STATE_PATH", self.state_path):
            state = {"identity": {"name": "persisto"}, "steps": {}}
            _save_onboard_state(state)
            loaded = _load_onboard_state()
            self.assertEqual(loaded["identity"]["name"], "persisto")

    def test_load_missing_returns_empty(self) -> None:
        with patch("onboard.ONBOARD_STATE_PATH", self.state_path):
            loaded = _load_onboard_state()
            self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest.main()
