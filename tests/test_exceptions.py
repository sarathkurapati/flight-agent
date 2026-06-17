"""Tests for agent/exceptions.py — hierarchy and attributes."""

import pytest

from agent.exceptions import (
    ActionParseError,
    AgentError,
    BrowserError,
    GoalFailedError,
    MaxStepsExceededError,
)


class TestExceptionHierarchy:
    def test_goal_failed_is_agent_error(self):
        assert issubclass(GoalFailedError, AgentError)

    def test_max_steps_is_agent_error(self):
        assert issubclass(MaxStepsExceededError, AgentError)

    def test_action_parse_is_agent_error(self):
        assert issubclass(ActionParseError, AgentError)

    def test_browser_error_is_agent_error(self):
        assert issubclass(BrowserError, AgentError)

    def test_all_are_exceptions(self):
        for cls in (
            AgentError,
            GoalFailedError,
            MaxStepsExceededError,
            ActionParseError,
            BrowserError,
        ):
            assert issubclass(cls, Exception)


class TestGoalFailedError:
    def test_reason_stored(self):
        exc = GoalFailedError("page requires login")
        assert exc.reason == "page requires login"

    def test_str_is_reason(self):
        exc = GoalFailedError("blocked by captcha")
        assert str(exc) == "blocked by captcha"

    def test_catchable_as_agent_error(self):
        with pytest.raises(AgentError):
            raise GoalFailedError("test")

    def test_catchable_as_goal_failed(self):
        with pytest.raises(GoalFailedError) as exc_info:
            raise GoalFailedError("no results found")
        assert exc_info.value.reason == "no results found"


class TestMaxStepsExceededError:
    def test_message(self):
        exc = MaxStepsExceededError("30 steps used")
        assert "30 steps" in str(exc)


class TestActionParseError:
    def test_message(self):
        exc = ActionParseError("bad json near line 3")
        assert "bad json" in str(exc)
