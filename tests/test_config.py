"""Tests for agent/config.py — validation, defaults, and directory creation."""

import pytest
from pydantic import ValidationError


class TestAgentConfigDefaults:
    def test_defaults_load_with_api_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("SCREENSHOTS_DIR", str(tmp_path / "shots"))
        monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
        from agent.config import AgentConfig

        cfg = AgentConfig()
        assert cfg.model == "claude-opus-4-8"
        assert cfg.max_steps == 30
        assert cfg.headless is True
        assert cfg.viewport_width == 1280
        assert cfg.viewport_height == 800
        assert cfg.api_retry_attempts == 3

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Ensure no .env file pollutes the test
        monkeypatch.chdir("/tmp")
        from agent.config import AgentConfig

        with pytest.raises(ValidationError, match="anthropic_api_key"):
            AgentConfig()

    def test_screenshots_dir_is_created(self, monkeypatch, tmp_path):
        shots = tmp_path / "new_shots"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.setenv("SCREENSHOTS_DIR", str(shots))
        monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
        from agent.config import AgentConfig

        cfg = AgentConfig()
        assert cfg.screenshots_dir.exists()

    def test_logs_dir_is_created(self, monkeypatch, tmp_path):
        logs = tmp_path / "new_logs"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.setenv("SCREENSHOTS_DIR", str(tmp_path / "shots"))
        monkeypatch.setenv("LOGS_DIR", str(logs))
        from agent.config import AgentConfig

        cfg = AgentConfig()
        assert cfg.logs_dir.exists()


class TestAgentConfigValidation:
    def _make(self, monkeypatch, tmp_path, **overrides):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.setenv("SCREENSHOTS_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LOGS_DIR", str(tmp_path / "l"))
        from agent.config import AgentConfig

        return AgentConfig(**overrides)

    def test_custom_model(self, monkeypatch, tmp_path):
        cfg = self._make(monkeypatch, tmp_path, model="claude-opus-4-8")
        assert cfg.model == "claude-opus-4-8"

    def test_custom_max_steps(self, monkeypatch, tmp_path):
        cfg = self._make(monkeypatch, tmp_path, max_steps=50)
        assert cfg.max_steps == 50

    def test_max_steps_too_low_raises(self, monkeypatch, tmp_path):
        with pytest.raises(ValidationError):
            self._make(monkeypatch, tmp_path, max_steps=0)

    def test_max_steps_too_high_raises(self, monkeypatch, tmp_path):
        with pytest.raises(ValidationError):
            self._make(monkeypatch, tmp_path, max_steps=101)

    def test_max_tokens_bounds(self, monkeypatch, tmp_path):
        with pytest.raises(ValidationError):
            self._make(monkeypatch, tmp_path, max_tokens=100)  # below 256

    def test_headless_flag(self, monkeypatch, tmp_path):
        cfg = self._make(monkeypatch, tmp_path, headless=True)
        assert cfg.headless is True
