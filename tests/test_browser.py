"""Tests for agent/browser.py — behaviour with a mocked Playwright stack."""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_playwright(tmp_path):
    """Patch sync_playwright so no real browser is launched."""
    mock_page = MagicMock()
    mock_page.url = "about:blank"
    mock_page.title.return_value = "Blank"
    mock_page.screenshot.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_browser = MagicMock()
    mock_browser.new_context.return_value = mock_context

    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser

    mock_pw_cm = MagicMock()
    mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_instance)
    mock_pw_cm.__exit__ = MagicMock(return_value=False)

    with patch("agent.browser.sync_playwright", return_value=mock_pw_instance):
        mock_pw_instance.start.return_value = mock_pw_instance
        yield {
            "playwright": mock_pw_instance,
            "browser": mock_browser,
            "context": mock_context,
            "page": mock_page,
            "tmp_path": tmp_path,
        }


@pytest.fixture()
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    from agent.config import AgentConfig

    return AgentConfig()


@pytest.fixture()
def browser(mock_playwright, config):
    from agent.browser import BrowserController

    return BrowserController(config=config, session_id="test_session")


class TestBrowserInit:
    def test_launches_chromium(self, mock_playwright, config):
        from agent.browser import BrowserController

        BrowserController(config=config, session_id="s1")
        mock_playwright["playwright"].chromium.launch.assert_called_once()

    def test_creates_screenshot_subdir(self, mock_playwright, config):
        from agent.browser import BrowserController

        BrowserController(config=config, session_id="s2")
        expected = config.screenshots_dir / "s2"
        assert expected.exists()

    def test_adds_webdriver_init_script(self, mock_playwright, config):
        from agent.browser import BrowserController

        BrowserController(config=config, session_id="s3")
        mock_playwright["context"].add_init_script.assert_called_once()
        script_arg = mock_playwright["context"].add_init_script.call_args[0][0]
        assert "webdriver" in script_arg


class TestBrowserActions:
    def test_navigate_calls_goto(self, browser, mock_playwright):
        browser.navigate("https://example.com")
        mock_playwright["page"].goto.assert_called_once_with(
            "https://example.com", wait_until="domcontentloaded"
        )

    def test_click_calls_mouse_click(self, browser, mock_playwright):
        browser.click(320, 240)
        mock_playwright["page"].mouse.click.assert_called_once_with(320, 240)

    def test_type_text_calls_keyboard_type(self, browser, mock_playwright):
        browser.type_text("hello")
        mock_playwright["page"].keyboard.type.assert_called_once_with("hello", delay=50)

    def test_press_key_calls_keyboard_press(self, browser, mock_playwright):
        browser.press_key("Enter")
        mock_playwright["page"].keyboard.press.assert_called_once_with("Enter")

    def test_scroll_down(self, browser, mock_playwright):
        browser.scroll("down", 2)
        mock_playwright["page"].mouse.wheel.assert_called_once_with(0, 600)

    def test_scroll_up(self, browser, mock_playwright):
        browser.scroll("up", 1)
        mock_playwright["page"].mouse.wheel.assert_called_once_with(0, -300)

    def test_wait_calls_wait_for_timeout(self, browser, mock_playwright):
        browser.wait(1.5)
        mock_playwright["page"].wait_for_timeout.assert_any_call(1500)


class TestBrowserObservation:
    def test_screenshot_returns_base64_string(self, browser, mock_playwright):
        result = browser.screenshot_base64()
        assert isinstance(result, str)
        # Must be valid base64
        decoded = base64.b64decode(result)
        assert len(decoded) > 0

    def test_screenshot_saves_file_to_session_dir(self, browser, config, mock_playwright):
        _FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        def _write_and_return(path=None, **_):
            if path:
                Path(path).write_bytes(_FAKE_PNG)
            return _FAKE_PNG

        mock_playwright["page"].screenshot.side_effect = _write_and_return
        browser.screenshot_base64()
        session_dir = config.screenshots_dir / "test_session"
        files = list(session_dir.glob("step_*.png"))
        assert len(files) == 1

    def test_screenshot_increments_step_counter(self, browser, config, mock_playwright):
        _FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        def _write_and_return(path=None, **_):
            if path:
                Path(path).write_bytes(_FAKE_PNG)
            return _FAKE_PNG

        mock_playwright["page"].screenshot.side_effect = _write_and_return
        browser.screenshot_base64()
        browser.screenshot_base64()
        session_dir = config.screenshots_dir / "test_session"
        files = sorted(session_dir.glob("step_*.png"))
        assert files[0].name.startswith("step_001_")
        assert files[1].name.startswith("step_002_")

    def test_current_url(self, browser, mock_playwright):
        mock_playwright["page"].url = "https://google.com"
        assert browser.current_url() == "https://google.com"

    def test_page_title(self, browser, mock_playwright):
        mock_playwright["page"].title.return_value = "Google"
        assert browser.page_title() == "Google"


class TestBrowserClose:
    def test_close_calls_browser_close(self, browser, mock_playwright):
        browser.close()
        mock_playwright["browser"].close.assert_called_once()

    def test_close_does_not_raise_on_error(self, browser, mock_playwright):
        mock_playwright["browser"].close.side_effect = RuntimeError("already closed")
        browser.close()  # should not raise


class TestBrowserActionErrors:
    """All action methods must raise BrowserError (not raw Playwright errors)."""

    def test_click_raises_browser_error_on_failure(self, browser, mock_playwright):
        from agent.exceptions import BrowserError

        mock_playwright["page"].mouse.click.side_effect = RuntimeError("element not found")
        with pytest.raises(BrowserError, match="Click"):
            browser.click(100, 200)

    def test_type_text_raises_browser_error_on_failure(self, browser, mock_playwright):
        from agent.exceptions import BrowserError

        mock_playwright["page"].keyboard.type.side_effect = RuntimeError("page closed")
        with pytest.raises(BrowserError, match="Type"):
            browser.type_text("hello")

    def test_press_key_raises_browser_error_on_failure(self, browser, mock_playwright):
        from agent.exceptions import BrowserError

        mock_playwright["page"].keyboard.press.side_effect = RuntimeError("detached")
        with pytest.raises(BrowserError, match="Press key"):
            browser.press_key("Enter")

    def test_scroll_raises_browser_error_on_failure(self, browser, mock_playwright):
        from agent.exceptions import BrowserError

        mock_playwright["page"].mouse.wheel.side_effect = RuntimeError("crash")
        with pytest.raises(BrowserError, match="Scroll"):
            browser.scroll("down", 2)

    def test_screenshot_raises_browser_error_on_failure(self, browser, mock_playwright):
        from agent.exceptions import BrowserError

        mock_playwright["page"].screenshot.side_effect = RuntimeError("page crashed")
        with pytest.raises(BrowserError, match="Screenshot"):
            browser.screenshot_base64()

    def test_page_title_raises_browser_error_on_failure(self, browser, mock_playwright):
        from agent.exceptions import BrowserError

        mock_playwright["page"].title.side_effect = RuntimeError("context destroyed")
        with pytest.raises(BrowserError, match="page title"):
            browser.page_title()
