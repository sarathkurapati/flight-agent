"""Integration tests for AgentSession.run() with mocked browser + API."""

import json
from unittest.mock import MagicMock

import pytest

from agent.core import AgentSession
from agent.exceptions import MaxStepsExceededError
from tests.conftest import make_mock_stream

# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _patch_browser(mocker):
    """Patch BrowserController so no real Chromium is launched."""
    instance = MagicMock()
    instance.screenshot_base64.return_value = "ZmFrZV9wbmc="  # base64 "fake_png"
    instance.current_url.return_value = "https://example.com"
    instance.page_title.return_value = "Example Domain"

    cls = mocker.patch("agent.core.BrowserController")
    cls.return_value = instance
    return instance


def _patch_anthropic(mocker, *stream_side_effects):
    """Patch anthropic.Anthropic to return a fake client."""
    mock_client = MagicMock()
    mock_client.messages.stream.side_effect = list(stream_side_effects)

    mocker.patch("agent.core.anthropic.Anthropic", return_value=mock_client)
    return mock_client


# ------------------------------------------------------------------ #
# Happy path                                                          #
# ------------------------------------------------------------------ #


class TestRunHappyPath:
    def test_returns_done_result(self, mocker, agent_config):
        _patch_browser(mocker)
        navigate = make_mock_stream(
            {"action": "navigate", "url": "https://google.com", "thought": "go"}
        )
        done = make_mock_stream(
            {"action": "done", "result": "Found the cheapest flight.", "thought": "done"}
        )
        _patch_anthropic(mocker, navigate, done)

        session = AgentSession(config=agent_config)
        result = session.run("find cheapest flight to Mumbai")

        assert result == "Found the cheapest flight."

    def test_result_does_not_start_with_failed(self, mocker, agent_config):
        _patch_browser(mocker)
        done = make_mock_stream({"action": "done", "result": "All done.", "thought": "finished"})
        _patch_anthropic(mocker, done)

        session = AgentSession(config=agent_config)
        result = session.run("simple task")

        assert not result.startswith("FAILED")

    def test_browser_is_closed_after_success(self, mocker, agent_config):
        browser = _patch_browser(mocker)
        done = make_mock_stream({"action": "done", "result": "ok", "thought": "done"})
        _patch_anthropic(mocker, done)

        AgentSession(config=agent_config).run("task")

        browser.close.assert_called_once()


# ------------------------------------------------------------------ #
# Goal failed                                                         #
# ------------------------------------------------------------------ #


class TestRunGoalFailed:
    def test_returns_failed_string(self, mocker, agent_config):
        _patch_browser(mocker)
        fail = make_mock_stream(
            {"action": "fail", "reason": "Login required.", "thought": "blocked"}
        )
        _patch_anthropic(mocker, fail)

        result = AgentSession(config=agent_config).run("buy ticket")

        assert result.startswith("FAILED")
        assert "Login required" in result

    def test_browser_closed_after_fail(self, mocker, agent_config):
        browser = _patch_browser(mocker)
        fail = make_mock_stream({"action": "fail", "reason": "CAPTCHA.", "thought": "blocked"})
        _patch_anthropic(mocker, fail)

        AgentSession(config=agent_config).run("task")

        browser.close.assert_called_once()


# ------------------------------------------------------------------ #
# Max steps exceeded                                                  #
# ------------------------------------------------------------------ #


class TestRunMaxSteps:
    def test_raises_max_steps_exceeded(self, mocker, agent_config):
        agent_config.max_steps = 2
        _patch_browser(mocker)

        navigate = make_mock_stream({"action": "navigate", "url": "https://x.com", "thought": "go"})
        _patch_anthropic(mocker, navigate, navigate, navigate)

        with pytest.raises(MaxStepsExceededError):
            AgentSession(config=agent_config).run("impossible task")

    def test_browser_closed_after_max_steps(self, mocker, agent_config):
        browser = _patch_browser(mocker)
        navigate = make_mock_stream({"action": "navigate", "url": "https://x.com", "thought": "go"})
        _patch_anthropic(mocker, *([navigate] * 35))

        with pytest.raises(MaxStepsExceededError):
            AgentSession(config=agent_config).run("endless task")

        browser.close.assert_called_once()


# ------------------------------------------------------------------ #
# Session JSON persistence                                            #
# ------------------------------------------------------------------ #


class TestSessionPersistence:
    def test_saves_session_json_on_success(self, mocker, agent_config):
        _patch_browser(mocker)
        done = make_mock_stream({"action": "done", "result": "saved", "thought": "done"})
        _patch_anthropic(mocker, done)

        AgentSession(config=agent_config).run("save this")

        logs = list(agent_config.logs_dir.glob("session_*.json"))
        assert len(logs) == 1

    def test_session_json_contains_goal(self, mocker, agent_config):
        _patch_browser(mocker)
        done = make_mock_stream({"action": "done", "result": "ok", "thought": "done"})
        _patch_anthropic(mocker, done)

        AgentSession(config=agent_config).run("my specific goal")

        log_path = next(agent_config.logs_dir.glob("session_*.json"))
        data = json.loads(log_path.read_text())
        assert data["goal"] == "my specific goal"
        assert data["failed"] is False

    def test_session_json_records_steps(self, mocker, agent_config):
        _patch_browser(mocker)
        navigate = make_mock_stream({"action": "navigate", "url": "https://x.com", "thought": "go"})
        done = make_mock_stream({"action": "done", "result": "ok", "thought": "done"})
        _patch_anthropic(mocker, navigate, done)

        AgentSession(config=agent_config).run("two step task")

        log_path = next(agent_config.logs_dir.glob("session_*.json"))
        data = json.loads(log_path.read_text())
        assert len(data["steps"]) == 2

    def test_session_json_marks_failed_on_fail_action(self, mocker, agent_config):
        _patch_browser(mocker)
        fail = make_mock_stream({"action": "fail", "reason": "blocked", "thought": "stuck"})
        _patch_anthropic(mocker, fail)

        AgentSession(config=agent_config).run("blocked task")

        log_path = next(agent_config.logs_dir.glob("session_*.json"))
        data = json.loads(log_path.read_text())
        assert data["failed"] is True


# ------------------------------------------------------------------ #
# Keyboard interrupt                                                  #
# ------------------------------------------------------------------ #


class TestKeyboardInterrupt:
    def test_returns_interrupted_string(self, mocker, agent_config):
        browser = _patch_browser(mocker)
        browser.screenshot_base64.side_effect = KeyboardInterrupt

        result = AgentSession(config=agent_config).run("task")

        assert "INTERRUPTED" in result

    def test_browser_closed_on_interrupt(self, mocker, agent_config):
        browser = _patch_browser(mocker)
        browser.screenshot_base64.side_effect = KeyboardInterrupt

        AgentSession(config=agent_config).run("task")

        browser.close.assert_called_once()


# ------------------------------------------------------------------ #
# Action parse error (graceful degradation)                          #
# ------------------------------------------------------------------ #


class TestActionParseError:
    def test_bad_json_skips_step_then_recovers(self, mocker, agent_config):
        """A garbled Claude response should not crash the session."""
        _patch_browser(mocker)

        bad_block = MagicMock()
        bad_block.type = "text"
        bad_block.text = "oops not json"
        bad_msg = MagicMock()
        bad_msg.content = [bad_block]
        bad_stream = MagicMock()
        bad_stream.get_final_message.return_value = bad_msg
        bad_cm = MagicMock()
        bad_cm.__enter__ = MagicMock(return_value=bad_stream)
        bad_cm.__exit__ = MagicMock(return_value=False)

        good_cm = make_mock_stream({"action": "done", "result": "recovered", "thought": "ok"})
        _patch_anthropic(mocker, bad_cm, good_cm)

        result = AgentSession(config=agent_config).run("recover from bad json")

        assert result == "recovered"


# ------------------------------------------------------------------ #
# New-fix coverage                                                    #
# ------------------------------------------------------------------ #


class TestGoalValidation:
    def test_goal_too_long_raises_value_error(self, agent_config):
        long_goal = "x" * (agent_config.max_goal_length + 1)
        with pytest.raises(ValueError, match="max_goal_length"):
            AgentSession(config=agent_config).run(long_goal)

    def test_goal_at_exact_limit_is_accepted(self, mocker, agent_config):
        _patch_browser(mocker)
        done = make_mock_stream({"action": "done", "result": "ok", "thought": "done"})
        _patch_anthropic(mocker, done)

        exact_goal = "x" * agent_config.max_goal_length
        result = AgentSession(config=agent_config).run(exact_goal)
        assert not result.startswith("FAILED")


class TestMissingActionFields:
    def test_click_without_coordinates_skips_step(self, mocker, agent_config):
        _patch_browser(mocker)
        bad_click = make_mock_stream({"action": "click", "thought": "click something"})
        done = make_mock_stream({"action": "done", "result": "recovered", "thought": "done"})
        _patch_anthropic(mocker, bad_click, done)

        result = AgentSession(config=agent_config).run("task")
        assert result == "recovered"

    def test_navigate_without_url_skips_step(self, mocker, agent_config):
        _patch_browser(mocker)
        bad_nav = make_mock_stream({"action": "navigate", "thought": "go somewhere"})
        done = make_mock_stream({"action": "done", "result": "ok", "thought": "done"})
        _patch_anthropic(mocker, bad_nav, done)

        result = AgentSession(config=agent_config).run("task")
        assert result == "ok"


class TestBrowserErrorRecovery:
    def test_browser_error_in_action_skips_step(self, mocker, agent_config):
        from agent.exceptions import BrowserError

        browser = _patch_browser(mocker)
        browser.navigate.side_effect = BrowserError("timeout")

        navigate = make_mock_stream({"action": "navigate", "url": "https://x.com", "thought": "go"})
        done = make_mock_stream({"action": "done", "result": "recovered", "thought": "done"})
        _patch_anthropic(mocker, navigate, done)

        result = AgentSession(config=agent_config).run("task")
        assert result == "recovered"

    def test_browser_closed_even_after_browser_error(self, mocker, agent_config):
        from agent.exceptions import BrowserError

        browser = _patch_browser(mocker)
        browser.navigate.side_effect = BrowserError("crash")

        navigate = make_mock_stream({"action": "navigate", "url": "https://x.com", "thought": "go"})
        done = make_mock_stream({"action": "done", "result": "ok", "thought": "done"})
        _patch_anthropic(mocker, navigate, done)

        AgentSession(config=agent_config).run("task")
        browser.close.assert_called_once()


class TestContextManager:
    def test_session_usable_as_context_manager(self, mocker, agent_config):
        _patch_browser(mocker)
        done = make_mock_stream({"action": "done", "result": "ctx ok", "thought": "done"})
        _patch_anthropic(mocker, done)

        with AgentSession(config=agent_config) as session:
            result = session.run("context manager task")

        assert result == "ctx ok"


class TestObservationErrorRecovery:
    def test_screenshot_failure_skips_step_then_recovers(self, mocker, agent_config):
        """A BrowserError during screenshot should skip the step, not crash the session."""
        from agent.exceptions import BrowserError

        browser = _patch_browser(mocker)
        browser.screenshot_base64.side_effect = [
            BrowserError("page crashed"),
            "ZmFrZV9wbmc=",  # recovers on next step
        ]
        done = make_mock_stream({"action": "done", "result": "recovered", "thought": "done"})
        _patch_anthropic(mocker, done)

        result = AgentSession(config=agent_config).run("task")
        assert result == "recovered"

    def test_browser_closed_after_observation_error(self, mocker, agent_config):
        from agent.exceptions import BrowserError

        agent_config.max_steps = 1
        browser = _patch_browser(mocker)
        browser.screenshot_base64.side_effect = BrowserError("crash")

        with pytest.raises(MaxStepsExceededError):
            AgentSession(config=agent_config).run("task")

        browser.close.assert_called_once()


class TestNonNumericCoordinates:
    def test_click_with_non_numeric_coords_skips_step(self, mocker, agent_config):
        """Claude returning non-numeric x/y should skip the step, not crash."""
        _patch_browser(mocker)
        bad_click = make_mock_stream(
            {"action": "click", "x": "center", "y": "top", "thought": "click"}
        )
        done = make_mock_stream({"action": "done", "result": "recovered", "thought": "done"})
        _patch_anthropic(mocker, bad_click, done)

        result = AgentSession(config=agent_config).run("task")
        assert result == "recovered"
