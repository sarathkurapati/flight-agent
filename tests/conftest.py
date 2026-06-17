"""Shared pytest fixtures for all test modules."""

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def agent_config(monkeypatch, tmp_path):
    """A valid AgentConfig pointing at tmp_path directories."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    from agent.config import AgentConfig

    return AgentConfig()


def make_mock_stream(action_dict: dict) -> MagicMock:
    """Build a fake context-manager stream that returns a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(action_dict)

    message = MagicMock()
    message.content = [block]

    stream = MagicMock()
    stream.get_final_message.return_value = message

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm
