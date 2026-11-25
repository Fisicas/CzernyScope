import io
import json
import os
import threading
import time
from urllib.error import URLError

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMainWindow, QPlainTextEdit, QPushButton

from spectrometer_app import updates


@pytest.fixture
def setup(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.capturing = False
    window.calibrating = False
    window.armed = None
    window.dialogs = set()
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    controller = updates.UpdateController(window, settings)
    window.show()
    try:
        yield app, window, settings, controller
    finally:
        controller.shutdown()
        window.close()
        app.processEvents()


def until(app, predicate):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the update controller")


def test_public_release_metadata_and_stable_versions(monkeypatch):
    payload = {
        "tag_name": "v0.10.1",
        "html_url": "https://github.com/Fisicas/czernyscope/releases/tag/v0.10.1",
        "body": "Changes",
    }

    def mock_urlopen(request, *, timeout):
        assert request.full_url == "https://api.github.com/repos/Fisicas/CzernyScope/releases/latest"
        assert request.get_header("Authorization") is None
        assert timeout == 6
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(updates, "urlopen", mock_urlopen)
    result = updates.fetch_latest_release()
    assert result == updates.Release("0.10.1", "https://github.com/Fisicas/CzernyScope/releases/tag/v0.10.1", "Changes")
    assert result.newer_than("0.9.9")
    assert not result.newer_than("0.10.1")
    payload["draft"] = True
    assert updates.fetch_latest_release() is None
    payload["draft"] = False
    payload["prerelease"] = True
    assert updates.fetch_latest_release() is None
    payload["prerelease"] = False
    payload["tag_name"] = "v0.10.1-rc1"
    assert updates.fetch_latest_release() is None
    payload["tag_name"] = "v0.10.1"
    payload["html_url"] = "https://example.org/Fisicas/CzernyScope/releases/tag/v0.10.1"
    with pytest.raises(updates.UpdateCheckError, match="unexpected"):
        updates.fetch_latest_release()


def test_daily_retry_and_quiet_offline_checks(setup, monkeypatch):
    app, window, settings, controller = setup
    calls = []
    window.statusBar().showMessage("Live status")

    def offline(*args, **kwargs):
        calls.append(threading.current_thread())
        raise URLError("fixture")

    monkeypatch.setattr(updates, "urlopen", offline)
    assert controller.automatic_action.isChecked()
    controller._check_scheduled()
    until(app, lambda: not controller.checking)
    assert calls and calls[0] is not threading.current_thread()
    assert controller.dialog is None
    assert window.statusBar().currentMessage() == "Live status"
    controller._check_scheduled()
    assert len(calls) == 1

    now = time.time()
    assert updates.check_due(now - updates.CHECK_INTERVAL_SECONDS, now)
    assert updates.check_due(now + 1, now)
    controller.automatic_action.trigger()
    assert settings.value("updates/automatic", type=bool) is False
    settings.setValue("updates/last_attempt", 0)
    controller._check_scheduled()
    assert len(calls) == 1

    controller.check_action.trigger()
    until(app, lambda: not controller.checking)
    assert controller.dialog is None
    assert "remains available offline" in window.statusBar().currentMessage()


def test_update_dialog_defers_and_respects_skip(setup, monkeypatch):
    app, window, settings, controller = setup
    release = updates.Release("99.0.0", f"{updates.REPOSITORY_URL}/releases/tag/v99.0.0", "<b>Plain notes</b>")
    monkeypatch.setattr(updates, "fetch_latest_release", lambda: release)
    opened = []
    monkeypatch.setattr(updates.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)

    window.capturing = True
    controller._check_scheduled()
    until(app, lambda: controller.pending is not None)
    assert controller.dialog is None
    window.capturing = False
    window.calibrating = True
    controller._deliver()
    assert controller.dialog is None
    window.calibrating = False
    window.armed = object()
    controller._deliver()
    assert controller.dialog is None
    window.armed = None
    controller._deliver()
    assert controller.dialog is not None and not controller.dialog.isModal()
    assert controller.dialog.findChild(QPlainTextEdit).toPlainText() == "<b>Plain notes</b>"
    next(button for button in controller.dialog.findChildren(QPushButton)
         if button.text() == "Skip this version").click()
    assert settings.value("updates/skipped_version") == release.version
    settings.setValue("updates/last_attempt", 0)
    controller._check_scheduled()
    until(app, lambda: not controller.checking)
    assert controller.dialog is None and opened == []

    controller.check_action.trigger()
    until(app, lambda: controller.dialog is not None)
    next(button for button in controller.dialog.findChildren(QPushButton)
         if button.text() == "View release…").click()
    assert opened == [release.page_url]
