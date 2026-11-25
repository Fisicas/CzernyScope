"""Exercise acquisition and persistence through the desktop controls."""

import csv
import math
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from spectrometer_app import hardware
from spectrometer_app.core import InstrumentInfo
from spectrometer_app.dialogs import DarkLibrariesDialog, DarkSessionDialog, WavelengthRangeDialog
from spectrometer_app.window import SpectrumWindow


def until(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the application")


def metadata(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return {row[0][2:]: row[1] for row in csv.reader(stream) if row[0].startswith("# ")}


def close_window(app, window):
    for dialog in tuple(window.dialogs):
        dialog.reject()
    window.close()
    until(app, lambda: not window.thread.isRunning() and not window.isVisible())


def enter_with_return(field, text):
    editor = field.lineEdit()
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, text)
    QTest.keyClick(editor, Qt.Key.Key_Return)


def test_demo_capture_dark_and_display(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = SpectrumWindow(tmp_path, "Maya2000Pro")
    window.show()
    try:
        until(app, lambda: window.plot.latest is not None and len(window.plot.frames) >= 2)
        assert window.windowTitle() == "CzernyScope"
        assert window.live_state.text() == "Simulation · Live"
        assert (window.count.value(), window.interval.value(), window.averages.value()) == (10, 0.5, 1)
        assert window.persistence.value() == window.plot.persistence_s == 1
        assert not window.auto_y_button.isChecked()
        window.auto_y_button.click()
        assert window.plot.auto_scale_y_action.isChecked()
        window.plot.auto_scale_y_action.trigger()
        assert not window.auto_y_button.isChecked()
        window.fit_y_button.click()
        window.fit_both_button.click()
        assert len(window.plot.history.get_segments()) > 0
        before = window.persistence.value()
        QTest.mouseClick(window.persistence_up, Qt.MouseButton.LeftButton, pos=QPoint(3, 3))
        assert window.persistence.value() == before + 1
        window.persistence.setValue(0)
        assert len(window.plot.history.get_segments()) == 0

        def set_manual_limits():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, WavelengthRangeDialog)
            dialog.lower.setText("0.4 µm")
            dialog.upper.setText("7,000 Å")
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(50, set_manual_limits)
        window.range_actions["manual"].trigger()
        assert window.plot.axes.get_xlim() == (400, 700)
        window.color_action.trigger()
        assert window.color_action.isChecked()
        window.range_actions["auto"].trigger()
        assert window.plot.axes.get_xlim() == (200, 1120)

        window.title.setText("Quick check")
        window.save_button.click()
        until(app, lambda: window.last_saved is not None and not window.capturing)
        quick = metadata(window.last_saved.details)
        assert quick["title"] == "Quick check"
        assert quick["backend"] == "demo"
        assert quick["dark_subtraction"] == "off"
        assert window.last_saved.raw.read_bytes().startswith(b"Wavelength (nm),Amplitude\r\n")
        assert window.last_saved.corrected.name.endswith("_corrected_identity.csv")
        assert window.last_saved.raw.read_bytes() == window.last_saved.corrected.read_bytes()

        window.title.setText("Repeat check")
        window.count.setText("2.5 spectra")
        assert not window.repeat_button.isEnabled()
        window.count.setText("3 spectra")
        window.interval.setText("2.225e2 ms")
        window.repeat_button.click()
        assert window.start_button.isEnabled() and "Armed · Repeat check" in window.capture_status.text()
        assert window.interval.text() == "0.22 s"
        assert window.is_live
        window.start_button.click()
        until(app, lambda: not window.capturing and window.capture_status.text().startswith("Saved 3"))
        series = sorted((metadata(path) for path in (window.directory / "Details").glob("*Repeat_check*.csv")),
                        key=lambda row: int(row["sequence"]))
        assert len(list(window.directory.glob("*Repeat_check*.csv"))) == 6
        assert len(series) == 3 and len({row["run_id"] for row in series}) == 1
        assert math.isclose(float(series[-1]["nominal_run_offset_s"]), 0.445)
        assert all(float(row["read_start_delay_from_due_s"]) >= 0 for row in series)
        starts = [float(row["read_start_offset_from_run_s"]) for row in series]
        assert all(second - first >= 0.2225 - 1e-8 for first, second in zip(starts, starts[1:]))

        def start_dark():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, DarkSessionDialog)
            dialog.label.setText("Room check")
            dialog.temperature.setText("70.7 °F")
            dialog.source.setCurrentIndex(dialog.source.findData("ambient"))
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(50, start_dark)
        window.dark_button.click()
        until(app, lambda: window.calibrating and window.dark_progress_group.isVisible())
        until(app, lambda: window.dark_progress.value() > 0)
        assert "/300 scans" in window.dark_progress_text.text()
        until(app, lambda: not window.calibrating and "Dark library saved" in window.statusBar().currentMessage(),
              timeout=32)
        assert not window.dark_progress_group.isVisible()
        active = window.dark_inventory.active
        assert active.label == "Room check"
        assert active.temperature_c == 21.5 and active.temperature_source == "ambient"
        assert window.dark_profile_label.text() == "Saved dark · Room check"

        def pin_dark():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, DarkLibrariesDialog)
            dialog.profile.setCurrentIndex(dialog.profile.findData(active.profile_id))
            assert "21.5 °C (ambient)" in dialog.summary.text()
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(50, pin_dark)
        window.dark_libraries_action.trigger()
        until(app, lambda: window.dark_inventory.selected_id == active.profile_id)
        window.subtract.click()
        until(app, lambda: window.plot.latest.corrected_counts is not None)
        flags = window.plot.latest.pixel_flags
        assert flags[177] == 1 and flags[1680] == -1
        assert window.interpolate.isChecked() and window.plot.interpolate_pixels
        window.plot.show_pixel_triangles_action.trigger()
        assert window.plot.interpolate_pixels
        assert not window.plot.hot.get_offsets().size and not window.plot.cold.get_offsets().size
        window.interpolate.click()
        assert not window.plot.interpolate_pixels
        window.pixels.click()
        window.pixels.click()
        assert window.interpolate.isChecked() and window.plot.interpolate_pixels
        assert window.dark_status.text().startswith("Subtraction active")
        window.title.setText("Corrected check")
        window.save_button.click()
        until(app, lambda: not window.capturing and "Corrected_check" in window.last_saved.raw.name)
        details = metadata(window.last_saved.details)
        assert details["dark_subtraction"] == "raw_counts - dark_counts"
        assert int(details["interpolated_channels"]) > 0
        assert window.last_saved.corrected.name.endswith("_corrected.csv")
        assert window.last_saved.raw.read_bytes() != window.last_saved.corrected.read_bytes()

        enter_with_return(window.integration, "1.00001e-1 s")
        until(app, lambda: bool(window.dialogs))
        dialog = next(iter(window.dialogs))
        assert "No saved dark" in dialog.windowTitle() and "100,001 µs" in dialog.text()
        assert window.plot.latest.integration_us == 100_001
        assert window.plot.latest.corrected_counts is None
        dialog.reject()
        assert window.integration.text() == "100 ms"
        QTest.keyClick(window.integration.lineEdit(), Qt.Key.Key_Return)
        assert window.integration.value() == 100.001
        window.subtract.setChecked(False)
        window.title.setText("Exact microseconds")
        window.save_button.click()
        until(app, lambda: not window.capturing and "Exact_microseconds" in window.last_saved.raw.name)
        assert metadata(window.last_saved.details)["integration_time_us"] == "100001"
        for notice in tuple(window.dialogs):
            notice.reject()
        window.auto_button.click()
        until(app, lambda: not window.auto_pending and window.plot.latest.integration_us % 10_000 == 0)
        assert window.plot.latest.integration_us in active.integration_us
        assert "." not in window.integration.text()
    finally:
        close_window(app, window)

    reopened = SpectrumWindow(tmp_path, "Maya2000Pro")
    reopened.show()
    try:
        until(app, lambda: reopened.plot.latest is not None and reopened.dark_inventory.active is not None)
        assert reopened.dark_inventory.active.profile_id == active.profile_id
        assert reopened.dark_inventory.selected_id == active.profile_id
        assert reopened.plot.latest.dark_record.profile_id == active.profile_id
    finally:
        close_window(app, reopened)


def test_maya_starts_in_documented_display_range(tmp_path, monkeypatch):
    wavelength = np.linspace(200, 1220, 2304)
    counts = np.full(wavelength.shape, 2700.0)
    counts[0], counts[2068:] = 3900, 0
    info = InstrumentInfo("Maya2000 Pro", "DUMMY", "pyseabreeze", 8000, 3_000_000, 65535)
    device = SimpleNamespace(
        info=info, wavelengths_nm=wavelength, set_integration_us=lambda value: value,
        read_counts=lambda: counts, close=lambda: None,
    )
    monkeypatch.setattr(hardware, "list_devices", lambda: [SimpleNamespace(label="Test fixture")])
    monkeypatch.setattr(hardware, "open_device", lambda _selection: device)
    app = QApplication.instance() or QApplication([])
    window = SpectrumWindow(tmp_path)
    window.show()
    try:
        until(app, lambda: window.connect_button.isEnabled())
        window.connect_button.click()
        until(app, lambda: window.plot.latest is not None)
        assert window.plot.axes.get_xlim() == (wavelength[10], wavelength[2057])
        assert window.plot.axes.get_ylim()[1] < counts[0]
        assert window.dark_inventory.active is None
        np.testing.assert_array_equal(window.plot.latest.raw_counts, counts)
        window.integration.setText("80 nm")
        assert not window.save_button.isEnabled()
        assert window.integration.value() == 100
        enter_with_return(window.integration, "0.08 s")
        until(app, lambda: window.plot.latest.integration_us == 80_000)
        assert window.integration.text() == "80 ms" and window.save_button.isEnabled()
        window.range_actions["full"].trigger()
        assert window.plot.axes.get_xlim() == (wavelength[0], wavelength[-1])
        window.range_actions["auto"].trigger()
        assert window.plot.axes.get_xlim() == (wavelength[10], wavelength[2057])
        counts[1150:1163] = info.max_counts
        until(app, lambda: window.saturation_status.isVisible())
        assert "13 channels" in window.saturation_status.text()
        counts[1150:1163] = 2700
        until(app, lambda: not window.saturation_status.isVisible())
    finally:
        close_window(app, window)


def test_averaged_series_and_cancel(tmp_path, monkeypatch):
    wavelength = np.linspace(400, 700, 16)
    info = InstrumentInfo("Array fixture", "AVERAGE-TEST", "test", 1000, 3_000_000, 65535)
    reads_started = []

    def read_counts():
        reads_started.append(time.monotonic())
        time.sleep(0.042)
        counts = np.full(wavelength.shape, 2000.0)
        if len(reads_started) % 3 == 0:
            counts[5] = info.max_counts
        return counts

    device = SimpleNamespace(
        info=info, wavelengths_nm=wavelength, set_integration_us=lambda value: value,
        read_counts=read_counts, close=lambda: None,
    )
    monkeypatch.setattr(hardware, "list_devices", lambda: [SimpleNamespace(label=info.model)])
    monkeypatch.setattr(hardware, "open_device", lambda _selection: device)
    app = QApplication.instance() or QApplication([])
    window = SpectrumWindow(tmp_path)
    window.show()
    try:
        until(app, window.connect_button.isEnabled)
        window.connect_button.click()
        until(app, lambda: window.plot.latest is not None)
        window.averages.setValue(3)
        until(app, lambda: window.plot.latest.scans_to_average == 3)
        assert "3-scan mean" in window.trace_status.text()
        assert window.plot.latest.raw_counts[5] < info.max_counts
        assert "1 of 3 scans" in window.saturation_status.toolTip()

        window.title.setText("Default cadence")
        window.repeat_button.click()
        window.start_button.click()
        until(app, lambda: window.capture_status.text().startswith("Saved 10"), timeout=10)
        assert len(list(window.directory.glob("*Default_cadence*.csv"))) == 20
        details = window.directory / "Details"
        records = sorted(
            (metadata(path) for path in details.glob("*Default_cadence*.csv")),
            key=lambda data: int(data["sequence"]),
        )
        assert [int(record["sequence"]) for record in records] == list(range(1, 11))
        assert all(record["scans_to_average"] == "3" for record in records)
        starts = [float(record["read_start_offset_from_run_s"]) for record in records]
        assert all(later - prior >= 0.5 for prior, later in zip(starts, starts[1:]))

        window.title.setText("Readout limited")
        window.count.setValue(3)
        window.interval.setValue(0.1)
        window.repeat_button.click()
        window.start_button.click()
        until(app, lambda: window.capture_status.text().startswith("Saved 3"))
        assert len(list(details.glob("*Readout_limited*.csv"))) == 3

        window.averages.setValue(100)
        window.title.setText("Cancelled mean")
        window.save_button.click()
        until(app, lambda: window.capture_status.text().startswith("Saving Cancelled mean"))
        read_count = len(reads_started)
        until(app, lambda: len(reads_started) > read_count)
        window.cancel_button.click()
        until(app, lambda: not window.capturing, timeout=1.5)
        assert not list(window.directory.glob("*Cancelled_mean*.csv"))
    finally:
        close_window(app, window)
