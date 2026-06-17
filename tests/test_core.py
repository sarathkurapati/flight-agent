"""Tests for agent/core.py — action parsing, detail formatting, ActionType enum."""

import json

import pytest

from agent.core import (
    ActionType,
    _action_detail,
    _cleanup_old_screenshots,
    _parse_action,
    _validate_action,
)
from agent.exceptions import ActionParseError


class TestParseAction:
    def test_plain_json(self):
        raw = '{"action": "navigate", "url": "https://example.com", "thought": "go"}'
        result = _parse_action(raw)
        assert result["action"] == "navigate"
        assert result["url"] == "https://example.com"

    def test_strips_markdown_fences(self):
        raw = '```json\n{"action": "click", "x": 100, "y": 200, "thought": "click button"}\n```'
        result = _parse_action(raw)
        assert result["action"] == "click"
        assert result["x"] == 100

    def test_strips_plain_fences(self):
        raw = '```\n{"action": "wait", "seconds": 2, "thought": "loading"}\n```'
        result = _parse_action(raw)
        assert result["action"] == "wait"

    def test_strips_leading_trailing_whitespace(self):
        raw = '   \n{"action": "done", "result": "all done", "thought": "finished"}\n  '
        result = _parse_action(raw)
        assert result["action"] == "done"

    def test_invalid_json_raises_action_parse_error(self):
        with pytest.raises(ActionParseError):
            _parse_action("not json at all")

    def test_empty_string_raises_action_parse_error(self):
        with pytest.raises(ActionParseError):
            _parse_action("")

    def test_all_action_fields_preserved(self):
        data = {
            "action": "scroll",
            "direction": "down",
            "amount": 5,
            "thought": "scroll to see more",
        }
        result = _parse_action(json.dumps(data))
        assert result == data

    def test_type_action(self):
        raw = '{"action": "type", "text": "hello world", "thought": "fill search"}'
        result = _parse_action(raw)
        assert result["text"] == "hello world"

    def test_fail_action(self):
        raw = '{"action": "fail", "reason": "page not found", "thought": "404"}'
        result = _parse_action(raw)
        assert result["reason"] == "page not found"


class TestActionDetail:
    def test_navigate(self):
        detail = _action_detail({"action": "navigate", "url": "https://google.com"})
        assert detail == "https://google.com"

    def test_click(self):
        detail = _action_detail({"action": "click", "x": 640, "y": 400})
        assert "640" in detail and "400" in detail

    def test_type_truncates_long_text(self):
        long = "a" * 100
        detail = _action_detail({"action": "type", "text": long})
        assert len(detail) < 60  # repr + truncation

    def test_type_short_text(self):
        detail = _action_detail({"action": "type", "text": "hello"})
        assert "hello" in detail

    def test_press_key(self):
        detail = _action_detail({"action": "press_key", "key": "Enter"})
        assert detail == "Enter"

    def test_scroll(self):
        detail = _action_detail({"action": "scroll", "direction": "down", "amount": 3})
        assert "down" in detail and "3" in detail

    def test_wait(self):
        detail = _action_detail({"action": "wait", "seconds": 2.5})
        assert "2.5" in detail

    def test_done(self):
        detail = _action_detail({"action": "done", "result": "Found the cheapest flight"})
        assert "cheapest" in detail

    def test_fail(self):
        detail = _action_detail({"action": "fail", "reason": "Login required"})
        assert "Login" in detail

    def test_unknown_action_returns_empty(self):
        detail = _action_detail({"action": "unknown_action"})
        assert detail == ""


class TestValidateAction:
    def test_navigate_valid(self):
        assert _validate_action({"action": "navigate", "url": "https://x.com"}) is None

    def test_click_valid(self):
        assert _validate_action({"action": "click", "x": 100, "y": 200}) is None

    def test_type_valid(self):
        assert _validate_action({"action": "type", "text": "hello"}) is None

    def test_press_key_valid(self):
        assert _validate_action({"action": "press_key", "key": "Enter"}) is None

    def test_navigate_missing_url(self):
        result = _validate_action({"action": "navigate"})
        assert result is not None
        assert "url" in result

    def test_click_missing_x(self):
        result = _validate_action({"action": "click", "y": 200})
        assert result is not None
        assert "x" in result

    def test_click_missing_y(self):
        result = _validate_action({"action": "click", "x": 100})
        assert result is not None
        assert "y" in result

    def test_type_missing_text(self):
        result = _validate_action({"action": "type"})
        assert result is not None
        assert "text" in result

    def test_press_key_missing_key(self):
        result = _validate_action({"action": "press_key"})
        assert result is not None
        assert "key" in result

    def test_scroll_needs_no_extra_fields(self):
        assert _validate_action({"action": "scroll", "direction": "down", "amount": 3}) is None

    def test_wait_needs_no_extra_fields(self):
        assert _validate_action({"action": "wait", "seconds": 2}) is None

    def test_done_needs_no_extra_fields(self):
        assert _validate_action({"action": "done", "result": "ok"}) is None

    def test_unknown_action_passes_validation(self):
        assert _validate_action({"action": "unknown"}) is None


class TestCleanupOldScreenshots:
    def test_does_nothing_under_limit(self, tmp_path):
        for i in range(3):
            (tmp_path / f"session_{i}").mkdir()
        _cleanup_old_screenshots(tmp_path, max_sessions=5)
        assert len(list(tmp_path.iterdir())) == 3

    def test_removes_oldest_dirs_over_limit(self, tmp_path):
        import time as _time

        dirs = []
        for i in range(5):
            d = tmp_path / f"session_{i}"
            d.mkdir()
            dirs.append(d)
            _time.sleep(0.01)  # ensure distinct mtime ordering

        _cleanup_old_screenshots(tmp_path, max_sessions=3)
        remaining = {p.name for p in tmp_path.iterdir()}
        assert "session_0" not in remaining  # oldest removed
        assert "session_1" not in remaining  # second oldest removed
        assert "session_4" in remaining  # newest kept

    def test_handles_missing_dir_gracefully(self, tmp_path):
        _cleanup_old_screenshots(tmp_path / "nonexistent", max_sessions=10)

    def test_only_removes_directories_not_files(self, tmp_path):
        (tmp_path / "stray_file.txt").write_text("x")
        for i in range(3):
            (tmp_path / f"session_{i}").mkdir()
        _cleanup_old_screenshots(tmp_path, max_sessions=2)
        assert (tmp_path / "stray_file.txt").exists()


class TestActionTypeEnum:
    def test_all_values(self):
        expected = {"navigate", "click", "type", "press_key", "scroll", "wait", "done", "fail"}
        actual = {m.value for m in ActionType}
        assert actual == expected

    def test_string_equality(self):
        assert ActionType.NAVIGATE == "navigate"
        assert ActionType.DONE == "done"
        assert ActionType.FAIL == "fail"

    def test_membership(self):
        assert "click" in [a.value for a in ActionType]
