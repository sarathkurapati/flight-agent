import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Writable data root — override with DATA_DIR env var.
# Defaults to /tmp/browser-agent (always writable in containers).
# Locally set DATA_DIR=. or override SCREENSHOTS_DIR/LOGS_DIR individually.
def _data_root() -> Path:
    return Path(os.getenv("DATA_DIR", "/tmp/browser-agent"))


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Anthropic ---
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    model: str = Field("claude-opus-4-8", description="Claude model to use")
    max_tokens: int = Field(4096, ge=256, le=32768)

    # --- Agent loop ---
    max_steps: int = Field(30, ge=1, le=100, description="Hard cap on ReAct iterations")
    session_timeout_seconds: int = Field(
        600, ge=60, description="Wall-clock cap for the entire session"
    )
    max_goal_length: int = Field(2000, ge=100, le=10_000, description="Max chars in a goal string")
    api_retry_attempts: int = Field(3, ge=1, le=10)
    api_retry_wait_seconds: float = Field(5.0, ge=1.0)

    # --- Browser ---
    headless: bool = Field(True, description="Run Chromium headless (always True in cloud)")
    viewport_width: int = Field(1280, ge=800)
    viewport_height: int = Field(800, ge=600)
    slow_mo_ms: int = Field(0, ge=0, description="Slow down Playwright actions (ms)")
    navigation_timeout_ms: int = Field(30_000, ge=5_000)
    action_delay_ms: int = Field(800, ge=0, description="Pause after each action (ms)")

    # --- Extended thinking ---
    thinking_budget_tokens: int | None = Field(
        None,
        ge=1024,
        le=10000,
        description="Enable extended thinking with this token budget. None = disabled.",
    )

    # --- Paths ---
    screenshots_dir: Path = Field(default_factory=lambda: _data_root() / "screenshots")
    logs_dir: Path = Field(default_factory=lambda: _data_root() / "logs")
    max_screenshot_sessions: int = Field(
        100, ge=10, description="Delete oldest session dirs when this count is exceeded"
    )

    @field_validator("screenshots_dir", "logs_dir", mode="after")
    @classmethod
    def _ensure_dir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v

    @model_validator(mode="after")
    def _thinking_budget_fits_max_tokens(self) -> "AgentConfig":
        if (
            self.thinking_budget_tokens is not None
            and self.thinking_budget_tokens >= self.max_tokens
        ):
            raise ValueError(
                f"thinking_budget_tokens ({self.thinking_budget_tokens}) must be less than "
                f"max_tokens ({self.max_tokens})"
            )
        return self
