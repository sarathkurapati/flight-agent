from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import anthropic
from loguru import logger
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .browser import BrowserController
from .config import AgentConfig
from .exceptions import (
    ActionParseError,
    BrowserError,
    GoalFailedError,
    MaxStepsExceededError,
)
from .prompts import SYSTEM_PROMPT

# Fields each action type must supply.
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "navigate": ["url"],
    "click": ["x", "y"],
    "type": ["text"],
    "press_key": ["key"],
}


class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    PRESS_KEY = "press_key"
    SCROLL = "scroll"
    WAIT = "wait"
    DONE = "done"
    FAIL = "fail"


@dataclass
class StepRecord:
    step: int
    action: str
    detail: str
    url: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class SessionState:
    session_id: str
    goal: str
    steps: list[StepRecord] = field(default_factory=list)
    result: str | None = None
    failed: bool = False

    def save(self, logs_dir: Path) -> None:
        path = logs_dir / f"session_{self.session_id}.json"
        with open(path, "w") as f:
            json.dump(
                {
                    "session_id": self.session_id,
                    "goal": self.goal,
                    "result": self.result,
                    "failed": self.failed,
                    "steps": [s.__dict__ for s in self.steps],
                },
                f,
                indent=2,
            )
        logger.debug("session state saved | path={}", path)


class AgentSession:
    """Runs a single goal as an autonomous browser session.

    Can be used as a context manager, though cleanup is guaranteed by run() itself:

        with AgentSession() as session:
            result = session.run("book cheapest flight to Mumbai")
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self._cfg = config or AgentConfig()  # type: ignore[call-arg]
        self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        self._retryer: Retrying = Retrying(
            retry=retry_if_exception_type(anthropic.APIError),
            stop=stop_after_attempt(self._cfg.api_retry_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._cfg.api_retry_wait_seconds,
                max=60,
            ),
            reraise=True,
        )

    def __enter__(self) -> AgentSession:
        return self

    def __exit__(self, *_: Any) -> None:
        pass  # browser lifecycle is scoped to each run() call

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def run(self, goal: str) -> str:
        if len(goal) > self._cfg.max_goal_length:
            raise ValueError(
                f"Goal is {len(goal)} chars — exceeds max_goal_length={self._cfg.max_goal_length}"
            )

        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        state = SessionState(session_id=session_id, goal=goal)

        logger.info("session_start | id={} goal={!r}", session_id, goal)
        _cleanup_old_screenshots(self._cfg.screenshots_dir, self._cfg.max_screenshot_sessions)

        browser = BrowserController(config=self._cfg, session_id=session_id)
        history: list[str] = []
        deadline = time.monotonic() + self._cfg.session_timeout_seconds

        try:
            for step in range(1, self._cfg.max_steps + 1):
                if time.monotonic() > deadline:
                    msg = (
                        f"FAILED: Session timeout ({self._cfg.session_timeout_seconds}s) exceeded."
                    )
                    state.failed = True
                    state.result = msg
                    state.save(self._cfg.logs_dir)
                    raise MaxStepsExceededError(msg)

                result = self._step(browser, step, goal, history, state)
                if result is not None:
                    state.result = result
                    state.save(self._cfg.logs_dir)
                    logger.info("session_done | id={} result={!r}", session_id, result)
                    return result
        except GoalFailedError as exc:
            failed_msg = f"FAILED: {exc.reason}"
            state.failed = True
            state.result = failed_msg
            state.save(self._cfg.logs_dir)
            logger.warning("goal_failed | id={} reason={!r}", session_id, exc.reason)
            return failed_msg
        except KeyboardInterrupt:
            logger.warning("interrupted | id={}", session_id)
            state.save(self._cfg.logs_dir)
            return "INTERRUPTED by user."
        finally:
            browser.close()

        exceeded_msg = "FAILED: Max steps reached without completing the goal."
        state.failed = True
        state.result = exceeded_msg
        state.save(self._cfg.logs_dir)
        raise MaxStepsExceededError(exceeded_msg)

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _step(
        self,
        browser: BrowserController,
        step: int,
        goal: str,
        history: list[str],
        state: SessionState,
    ) -> str | None:
        try:
            screenshot = browser.screenshot_base64()
            url = browser.current_url()
            title = browser.page_title()
        except BrowserError as exc:
            logger.warning("observation_error | step={} err={}", step, exc)
            history.append(f"Step {step}: (observation error — {str(exc)[:80]})")
            return None

        logger.info("step={} url={!r} title={!r}", step, url, title[:60])

        # --- Ask Claude ---
        try:
            action = self._call_claude(goal, step, url, title, screenshot, history)
        except ActionParseError as exc:
            logger.warning("parse_error | step={} err={}", step, exc)
            history.append(f"Step {step}: (parse error, skipping)")
            return None

        action_type = action.get("action", "")
        thought = action.get("thought", "")

        logger.debug("thought={!r}", thought)
        logger.info("action={} detail={}", action_type, _action_detail(action))

        # --- Validate required fields before touching the browser ---
        validation_error = _validate_action(action)
        if validation_error:
            logger.warning("invalid_action | step={} err={}", step, validation_error)
            history.append(f"Step {step}: (invalid action — {validation_error})")
            return None

        record = StepRecord(
            step=step,
            action=action_type,
            detail=_action_detail(action),
            url=url,
        )
        state.steps.append(record)

        if action_type == ActionType.DONE:
            result = str(action.get("result", "Goal completed."))
            history.append(f"Step {step}: done — {result}")
            return result

        if action_type == ActionType.FAIL:
            raise GoalFailedError(str(action.get("reason", "Unspecified failure.")))

        # --- Execute browser action, recover from transient browser errors ---
        try:
            self._execute(browser, action)
        except (BrowserError, ValueError, TypeError) as exc:
            logger.warning("browser_error | step={} err={}", step, exc)
            history.append(f"Step {step}: (browser error — {str(exc)[:80]})")
            return None

        history.append(f"Step {step}: {action_type}({_action_detail(action)})")
        if len(history) > 6:
            history.pop(0)

        time.sleep(0.1)  # brief buffer for SPA DOM updates before next screenshot
        return None

    def _execute(self, browser: BrowserController, action: dict[str, Any]) -> None:
        action_type = action.get("action", "")

        if action_type == ActionType.NAVIGATE:
            browser.navigate(action["url"])
        elif action_type == ActionType.CLICK:
            browser.click(int(action["x"]), int(action["y"]))
        elif action_type == ActionType.TYPE:
            browser.type_text(action["text"])
        elif action_type == ActionType.PRESS_KEY:
            browser.press_key(action["key"])
        elif action_type == ActionType.SCROLL:
            browser.scroll(action.get("direction", "down"), int(action.get("amount", 3)))
        elif action_type == ActionType.WAIT:
            browser.wait(float(action.get("seconds", 2)))
        else:
            logger.warning("unknown action type: {!r}", action_type)

    # ------------------------------------------------------------------ #
    # Claude API call — retries wired to config                           #
    # ------------------------------------------------------------------ #

    def _call_claude(
        self,
        goal: str,
        step: int,
        url: str,
        title: str,
        screenshot_b64: str,
        history: list[str],
    ) -> dict[str, Any]:
        return self._retryer(self._api_request, goal, step, url, title, screenshot_b64, history)  # type: ignore[return-value]

    def _api_request(
        self,
        goal: str,
        step: int,
        url: str,
        title: str,
        screenshot_b64: str,
        history: list[str],
    ) -> dict[str, Any]:
        history_text = "\n".join(history[-6:]) if history else "None yet."
        user_text = (
            f"Goal: {goal}\n"
            f"Step: {step}/{self._cfg.max_steps}\n"
            f"Current URL: {url}\n"
            f"Page title: {title}\n\n"
            f"Recent actions:\n{history_text}\n\n"
            "What is the next action?"
        )

        kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "max_tokens": self._cfg.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        },
                    ],
                }
            ],
        }
        if self._cfg.thinking_budget_tokens is not None:
            kwargs["thinking"] = {  # type: ignore[assignment]
                "type": "enabled",
                "budget_tokens": self._cfg.thinking_budget_tokens,
            }
        with self._client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()

        raw = ""
        for block in message.content:
            if block.type == "text":
                raw = block.text
                break

        return _parse_action(raw)


# ------------------------------------------------------------------ #
# Module-level helpers                                                #
# ------------------------------------------------------------------ #


def _validate_action(action: dict[str, Any]) -> str | None:
    """Return an error string if the action is missing required fields, else None."""
    action_type = action.get("action", "")
    for required in _REQUIRED_FIELDS.get(action_type, []):
        if required not in action:
            return f"'{action_type}' missing required field '{required}'"
    return None


def _parse_action(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        return cast(dict[str, Any], json.loads(raw.strip()))
    except json.JSONDecodeError as exc:
        raise ActionParseError(
            f"Could not parse Claude response as JSON: {exc}\nRaw: {raw[:300]}"
        ) from exc


def _action_detail(action: dict[str, Any]) -> str:
    t = action.get("action", "")
    if t == ActionType.NAVIGATE:
        return str(action.get("url", ""))
    if t == ActionType.CLICK:
        return f"x={action.get('x')} y={action.get('y')}"
    if t == ActionType.TYPE:
        return repr(str(action.get("text", ""))[:40])
    if t == ActionType.PRESS_KEY:
        return str(action.get("key", ""))
    if t == ActionType.SCROLL:
        return f"{action.get('direction')} x{action.get('amount')}"
    if t == ActionType.WAIT:
        return f"{action.get('seconds')}s"
    if t == ActionType.DONE:
        return str(action.get("result", ""))[:60]
    if t == ActionType.FAIL:
        return str(action.get("reason", ""))[:60]
    return ""


def _cleanup_old_screenshots(screenshots_dir: Path, max_sessions: int) -> None:
    """Remove the oldest session subdirectories when count exceeds max_sessions."""
    if not screenshots_dir.exists():
        return
    session_dirs = sorted(
        (p for p in screenshots_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    excess = len(session_dirs) - max_sessions
    if excess <= 0:
        return
    for old_dir in session_dirs[:excess]:
        shutil.rmtree(old_dir, ignore_errors=True)
        logger.debug("removed old screenshot dir | path={}", old_dir)
