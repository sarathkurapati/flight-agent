from __future__ import annotations

import base64
from contextlib import suppress
from datetime import datetime

from loguru import logger
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import AgentConfig
from .exceptions import BrowserError

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


class BrowserController:
    """Thin Playwright wrapper with screenshot persistence and anti-detection."""

    def __init__(self, config: AgentConfig, session_id: str) -> None:
        self._cfg = config
        self._session_id = session_id
        self._screenshot_dir = config.screenshots_dir / session_id
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._step = 0

        self._pw = sync_playwright().start()
        try:
            self._browser: Browser = self._pw.chromium.launch(
                headless=config.headless,
                slow_mo=config.slow_mo_ms,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context: BrowserContext = self._browser.new_context(
                viewport={"width": config.viewport_width, "height": config.viewport_height},
                user_agent=_USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
            )
            # Remove webdriver fingerprint
            self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self._page: Page = self._context.new_page()
            self._page.set_default_timeout(config.navigation_timeout_ms)
        except Exception as exc:
            with suppress(Exception):
                self._pw.stop()
            raise BrowserError(f"Failed to launch browser: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Navigation                                                           #
    # ------------------------------------------------------------------ #

    def navigate(self, url: str) -> None:
        logger.debug("navigate | url={}", url)
        try:
            self._page.goto(url, wait_until="domcontentloaded")
            self._page.wait_for_timeout(self._cfg.action_delay_ms)
        except Exception as exc:
            raise BrowserError(f"Navigation failed for {url}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Input actions                                                        #
    # ------------------------------------------------------------------ #

    def click(self, x: int, y: int) -> None:
        logger.debug("click | x={} y={}", x, y)
        self._page.mouse.click(x, y)
        self._page.wait_for_timeout(self._cfg.action_delay_ms)

    def type_text(self, text: str) -> None:
        logger.debug("type | text={!r}", text[:60])
        self._page.keyboard.type(text, delay=50)
        self._page.wait_for_timeout(self._cfg.action_delay_ms // 2)

    def press_key(self, key: str) -> None:
        logger.debug("press_key | key={}", key)
        self._page.keyboard.press(key)
        self._page.wait_for_timeout(self._cfg.action_delay_ms)

    def scroll(self, direction: str, amount: int = 3) -> None:
        logger.debug("scroll | direction={} amount={}", direction, amount)
        delta = -300 * amount if direction == "up" else 300 * amount
        self._page.mouse.wheel(0, delta)
        self._page.wait_for_timeout(self._cfg.action_delay_ms)

    def wait(self, seconds: float) -> None:
        logger.debug("wait | seconds={}", seconds)
        self._page.wait_for_timeout(int(seconds * 1_000))

    # ------------------------------------------------------------------ #
    # Observation                                                          #
    # ------------------------------------------------------------------ #

    def screenshot_base64(self) -> str:
        self._step += 1
        ts = datetime.now().strftime("%H%M%S")
        path = self._screenshot_dir / f"step_{self._step:03d}_{ts}.png"
        png_bytes = self._page.screenshot(path=str(path), full_page=False)
        logger.debug("screenshot saved | path={}", path)
        return base64.b64encode(png_bytes).decode("utf-8")

    def current_url(self) -> str:
        return self._page.url

    def page_title(self) -> str:
        return self._page.title()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        with suppress(Exception):
            self._browser.close()
        with suppress(Exception):
            self._pw.stop()
        logger.debug("browser closed")
