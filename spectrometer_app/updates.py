"""Check the stable release channel without modifying the installation."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, QSettings, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from . import APP_NAME, __version__


REPOSITORY_URL = "https://github.com/Fisicas/CzernyScope"
RELEASE_API = "https://api.github.com/repos/Fisicas/CzernyScope/releases/latest"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
MAX_RESPONSE_BYTES = 1024 * 1024
_STABLE_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class UpdateCheckError(RuntimeError):
    """A check could not determine the latest stable release."""


def stable_version(value: str) -> tuple[int, int, int]:
    match = _STABLE_VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Expected a stable major.minor.patch version.")
    return tuple(int(part) for part in match.groups())


def check_due(last_attempt, now: float) -> bool:
    try:
        elapsed = now - float(last_attempt)
    except (TypeError, ValueError, OverflowError):
        return True
    return not 0 <= elapsed < CHECK_INTERVAL_SECONDS


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    notes: str

    def newer_than(self, installed: str = __version__) -> bool:
        return stable_version(self.version) > stable_version(installed)


def fetch_latest_release(timeout: float = 6.0) -> Release | None:
    """Read bounded metadata from the public GitHub release endpoint."""
    request = Request(RELEASE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code == 404:
            message = "No published stable CzernyScope release was found."
        elif error.code in (403, 429):
            message = "GitHub refused or rate-limited the update check. Try again later."
        else:
            message = f"GitHub returned HTTP {error.code}. Try again later."
        raise UpdateCheckError(message) from None
    except (URLError, OSError):
        raise UpdateCheckError("Could not reach GitHub. CzernyScope remains available offline.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("GitHub returned an oversized release response.")
    try:
        metadata = json.loads(raw)
    except (UnicodeError, ValueError):
        raise UpdateCheckError("GitHub returned invalid release information.") from None
    if not isinstance(metadata, dict):
        raise UpdateCheckError("GitHub returned invalid release information.")
    if metadata.get("draft") or metadata.get("prerelease"):
        return None
    tag = metadata.get("tag_name")
    try:
        stable_version(tag)
    except ValueError:
        return None
    canonical_url = f"{REPOSITORY_URL}/releases/tag/{tag}"
    reported_url = metadata.get("html_url")
    if not isinstance(reported_url, str) or reported_url.casefold() != canonical_url.casefold():
        raise UpdateCheckError("GitHub returned an unexpected release address.")
    notes = metadata.get("body")
    if notes is not None and not isinstance(notes, str):
        raise UpdateCheckError("GitHub returned invalid release notes.")
    return Release(tag.removeprefix("v"), canonical_url, notes or "No release notes were provided.")


class UpdateController(QObject):
    completed = Signal(object, str, bool)

    def __init__(self, window, settings=None):
        super().__init__(window)
        self.window = window
        self.settings = settings if settings is not None else QSettings(APP_NAME, APP_NAME)
        self.closed = False
        self.checking = False
        self.pending = None
        self.dialog = None
        self.completed.connect(self._completed)

        self.check_action = QAction("Check for updates…", self)
        self.check_action.triggered.connect(lambda: self.check(manual=True))
        self.automatic_action = QAction("Check for updates automatically", self)
        self.automatic_action.setCheckable(True)
        self.automatic_action.setChecked(self.settings.value("updates/automatic", True, type=bool))
        self.automatic_action.toggled.connect(self._set_automatic)

        # A workstation can keep the application open across several days.
        self.schedule = QTimer(self)
        self.schedule.setInterval(60 * 60 * 1000)
        self.schedule.timeout.connect(self._check_scheduled)
        self.retry = QTimer(self)
        self.retry.setInterval(1000)
        self.retry.timeout.connect(self._deliver)

    def start(self):
        self.schedule.start()
        QTimer.singleShot(1500, self, self._check_scheduled)

    def _set_automatic(self, enabled):
        self.settings.setValue("updates/automatic", enabled)
        self.settings.sync()
        if enabled and self.schedule.isActive():
            self._check_scheduled()

    def _check_scheduled(self):
        if check_due(self.settings.value("updates/last_attempt"), time.time()):
            self.check()

    def check(self, *, manual=False):
        if self.closed or self.checking or self.pending is not None or self.dialog is not None:
            return
        if not manual and not self.automatic_action.isChecked():
            return
        self.checking = True
        self.check_action.setEnabled(False)
        self.settings.setValue("updates/last_attempt", time.time())
        self.settings.sync()
        if manual and not self._busy():
            self.window.statusBar().showMessage("Checking for a stable CzernyScope release…")

        def work():
            release, error = None, ""
            try:
                release = fetch_latest_release()
            except UpdateCheckError as exc:
                error = str(exc)
            except Exception:
                error = "The update check failed. Try again later."
            try:
                self.completed.emit(release, error, manual)
            except RuntimeError:
                pass  # The window may have closed during the network request.

        threading.Thread(target=work, name="CzernyScope-update-check", daemon=True).start()

    @Slot(object, str, bool)
    def _completed(self, release, error, manual):
        self.checking = False
        if self.closed:
            return
        newer = release is not None and release.newer_than()
        if not manual and (not newer or not self.automatic_action.isChecked()
                           or self.settings.value("updates/skipped_version") == release.version):
            self.check_action.setEnabled(True)
            return
        self.pending = (release if newer else None, error, manual)
        self._deliver()

    def _busy(self):
        return (self.window.capturing or self.window.calibrating or self.window.armed is not None
                or bool(self.window.dialogs) or QApplication.activeModalWidget() is not None)

    def _deliver(self):
        if self.closed or self.pending is None:
            return
        if self._busy():
            if not self.retry.isActive():
                self.retry.start()
            return
        self.retry.stop()
        release, error, manual = self.pending
        self.pending = None
        if not manual and not self.automatic_action.isChecked():
            self.check_action.setEnabled(True)
            return
        if release is None:
            self.window.statusBar().showMessage(error or "No newer stable CzernyScope release is available.", 12000)
            self.check_action.setEnabled(True)
            return
        self._show_release(release)

    def _show_release(self, release):
        dialog = QDialog(self.window)
        self.dialog = dialog
        self.window.dialogs.add(dialog)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("CzernyScope update available")
        dialog.resize(520, 360)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"CzernyScope {release.version} is available. You have {__version__}.")
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)
        notes = QPlainTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(release.notes)
        layout.addWidget(notes, 1)
        hint = QLabel("View release opens GitHub in your browser. Close CzernyScope before running the installer.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox()
        view = buttons.addButton("View release…", QDialogButtonBox.ButtonRole.AcceptRole)
        later = buttons.addButton("Later", QDialogButtonBox.ButtonRole.RejectRole)
        skip = buttons.addButton("Skip this version", QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)

        def open_page():
            if QDesktopServices.openUrl(QUrl(release.page_url)):
                dialog.accept()
            else:
                self.window.statusBar().showMessage("Could not open the release page in your browser.", 12000)

        def skip_version():
            self.settings.setValue("updates/skipped_version", release.version)
            self.settings.sync()
            dialog.reject()

        def finished(_result):
            self.window.dialogs.discard(dialog)
            self.dialog = None
            if not self.closed:
                self.check_action.setEnabled(True)

        view.clicked.connect(open_page)
        later.clicked.connect(dialog.reject)
        skip.clicked.connect(skip_version)
        dialog.finished.connect(finished)
        dialog.show()

    def shutdown(self):
        self.closed = True
        self.schedule.stop()
        self.retry.stop()
        self.pending = None
        if self.dialog is not None:
            self.dialog.reject()
