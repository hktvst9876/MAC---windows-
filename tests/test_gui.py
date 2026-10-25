"""End-to-end tests of the whole application (offscreen Qt)."""

import asyncio
import os
import shutil
import threading
import time

import pytest

from mirrorbridge.config import AppConfig

CHROMIUM = os.environ.get("CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("google-chrome")
if not CHROMIUM and os.path.exists("/opt/pw-browsers"):
    for root, _, files in os.walk("/opt/pw-browsers"):
        if "chrome" in files and "headless" not in root:
            CHROMIUM = os.path.join(root, "chrome")
            break


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app_env(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    from mirrorbridge.core.controller import Controller
    from mirrorbridge.ui.main_window import MainWindow

    cfg = AppConfig.load()
    cfg.name = "Test Receiver"
    cfg.port = 0
    cfg.webcast_port = 0
    cfg.audio_enabled = False
    cfg.record_folder = str(tmp_path / "rec")
    cfg.screenshot_folder = str(tmp_path / "shots")
    controller = Controller(cfg)
    window = MainWindow(controller, cfg)
    window.show()
    controller.start()
    yield qapp, controller, window, tmp_path
    window._quitting = True
    for win in list(window.mirror_windows.values()):
        win._closing_from_session = True
        win.close()
    controller.shutdown()


def wait_for(qapp, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_airplay_mirroring_through_the_ui(app_env):
    qapp, controller, window, tmp_path = app_env
    assert wait_for(qapp, lambda: controller.status[0] == "Ready")
    from mirrorbridge.testing.fake_sender import run as run_sender

    thread = threading.Thread(target=lambda: asyncio.run(
        run_sender("127.0.0.1", controller.port, 4, 640, 360, 30, name="Anna's iPhone")), daemon=True)
    thread.start()
    assert wait_for(qapp, lambda: len(window.mirror_windows) == 1)
    session = list(controller.sessions.values())[0]
    mirror = window.mirror_windows[session.id]
    assert session.display_name == "Anna's iPhone"
    assert wait_for(qapp, lambda: mirror.video._image is not None)
    assert (session.codec, session.width, session.height) == ("h264", 640, 360)

    shot = session.screenshot()
    assert shot.exists() and shot.read_bytes()[:4] == b"\x89PNG"
    path = session.start_recording()
    wait_for(qapp, lambda: False, 1.5)
    session.stop_recording()
    assert path.exists() and path.stat().st_size > 10_000
    assert mirror.grab().width() > 0

    thread.join(15)
    assert wait_for(qapp, lambda: not controller.sessions and not window.rows)


@pytest.mark.skipif(not CHROMIUM, reason="Chromium not available")
def test_browser_cast_with_real_chromium(app_env):
    playwright = pytest.importorskip("playwright.sync_api")
    qapp, controller, window, tmp_path = app_env
    assert wait_for(qapp, lambda: controller.status[0] == "Ready" and controller.webcast is not None)
    result = {}

    def browse():
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=[
                "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--auto-select-desktop-capture-source=Entire screen"])
            page = browser.new_context(ignore_https_errors=True).new_page()
            page.goto("https://127.0.0.1:%d/" % controller.webcast.port)
            page.fill("#devname", "Test Chromebook")
            page.click("#start")
            page.wait_for_selector("#status.live", timeout=10000)
            time.sleep(3)
            result["status"] = page.inner_text("#status")
            page.click("#stop")
            time.sleep(1)
            browser.close()

    thread = threading.Thread(target=browse, daemon=True)
    thread.start()
    assert wait_for(qapp, lambda: len(controller.sessions) == 1, 20)
    session = list(controller.sessions.values())[0]
    assert session.display_name == "Test Chromebook"
    assert wait_for(qapp, lambda: session.decoder.frames > 10, 15)
    assert session.has_audio
    thread.join(30)
    assert "Casting" in result.get("status", "")
    assert wait_for(qapp, lambda: not controller.sessions, 10)
