"""Serialize spectrometer operations and disk writes away from the GUI thread."""

from __future__ import annotations

import math
import time
from concurrent.futures import CancelledError
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from .core import (
    DarkLibrary, DarkProfileSummary, DemoDevice, RepeatSchedule, UsableBounds, acquire_frame, infer_usable_bounds_nm,
    save_spectrum_pair, suggest_integration_us,
)
from .quantities import format_exposure


@dataclass(frozen=True)
class Capture:
    directory: Path
    title: str
    count: int
    interval_s: float
    interpolate_pixels: bool = False
    scans_to_average: int = 1


@dataclass(frozen=True)
class DarkProgress:
    done: int
    total: int
    integration_us: int
    requested_s: float
    remaining_s: float


@dataclass(frozen=True)
class DarkInventory:
    profiles: tuple[DarkProfileSummary, ...]
    selected_id: str | None
    active: DarkProfileSummary | None
    usable_bounds: UsableBounds | None


class AcquisitionWorker(QObject):
    inventory = Signal(object)
    connected = Signal(object)
    disconnected = Signal()
    frame_ready = Signal(object)
    live_changed = Signal(bool)
    capture_changed = Signal(str, bool)
    saved = Signal(object)
    dark_changed = Signal(str, bool)
    dark_progress = Signal(object)
    dark_inventory = Signal(object)
    dark_failed = Signal(str)
    exposure_changed = Signal(int)
    auto_finished = Signal()
    error = Signal(str, str)
    finished = Signal()

    def __init__(self, dark_directory: Path):
        super().__init__()
        self.library = DarkLibrary(dark_directory)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.device = None
        self.running = False
        self.integration_us = 100_000
        self.scans_to_average = 1
        self._read_stop = Event()
        self._closing = Event()
        self.subtract_dark = False
        self.capture = None
        self.schedule = None
        self.dark_session = None
        self.dark_requested_s = 0.0
        self.dark_state = DarkInventory((), None, None, None)
        self.auto_remaining = 0
        self.last_frame_emit = -math.inf
        self.read_elapsed_s = 0
        self.dark_failure = None

    @Slot()
    def scan(self):
        if self.device is not None:
            return
        try:
            from .hardware import list_devices

            self.inventory.emit(list_devices())
        except Exception as exc:
            self.error.emit("Device scan", str(exc))
            self.inventory.emit([])

    @Slot(object)
    def connect_device(self, selection):
        if self.device is not None:
            return
        try:
            if isinstance(selection, str):
                self.device = DemoDevice(selection)
            else:
                from .hardware import open_device

                self.device = open_device(selection)
            info = self.device.info
            self.integration_us = self.device.set_integration_us(
                self._supported_exposure(100_000)
            )
            self.last_frame_emit = -math.inf
            self.dark_failure = None
            self.connected.emit(info)
            self.exposure_changed.emit(self.integration_us)
            self._refresh_dark_inventory()
            self.set_live(True)
        except Exception as exc:
            self._close_device()
            self.error.emit("Open spectrometer", str(exc))

    @Slot()
    def disconnect(self):
        self.timer.stop()
        self.set_live(False)
        self._end_capture("Capture canceled")
        self._cancel_dark()
        self._close_device()

    def _close_device(self):
        self.dark_state = DarkInventory((), None, None, None)
        if self.device is not None:
            device, self.device = self.device, None
            try:
                device.close()
            except Exception as exc:
                self.error.emit("Close spectrometer", str(exc))
        self.disconnected.emit()

    @Slot()
    def shutdown(self):
        self.disconnect()
        self.finished.emit()

    @Slot(bool)
    def set_live(self, enabled):
        self.running = bool(enabled and self.device is not None)
        if self.running:
            self._read_stop.clear()
        else:
            self._stop_auto()
            if self.dark_session is None:
                self.timer.stop()
        self.live_changed.emit(self.running)
        if self.running and not self.timer.isActive():
            self.timer.start(0)

    def _supported_exposure(self, target):
        info = self.device.info
        steps = round((target - info.min_integration_us) / info.integration_step_us)
        max_steps = (info.max_integration_us - info.min_integration_us) // info.integration_step_us
        return info.min_integration_us + min(max(0, steps), max_steps) * info.integration_step_us

    @Slot(int)
    def set_integration(self, requested_us):
        if self.device is None or self.dark_session is not None or self.capture is not None:
            return
        self._stop_auto()
        try:
            self.integration_us = self.device.set_integration_us(
                self._supported_exposure(requested_us)
            )
            self.read_elapsed_s = 0
            self.exposure_changed.emit(self.integration_us)
        except Exception as exc:
            self.error.emit("Set integration time", str(exc))
            self.exposure_changed.emit(self.integration_us)
        finally:
            self._read_stop.clear()

    @Slot(int)
    def set_averaging(self, scans):
        if self.capture is None and self.dark_session is None:
            self.scans_to_average = scans
            self.read_elapsed_s = 0
            self._read_stop.clear()

    @Slot(bool)
    def set_subtraction(self, subtract):
        if self.capture is None and self.dark_session is None:
            self.subtract_dark = subtract

    @Slot()
    def auto_expose(self):
        if self.device is not None and self.dark_session is None and self.capture is None:
            self.auto_remaining = 5
            self.set_live(True)

    def _stop_auto(self):
        if self.auto_remaining:
            self.auto_remaining = 0
            self.auto_finished.emit()

    def _adjust_integration(self, frame):
        active = self.dark_state.active
        usable = self.dark_state.usable_bounds
        updated = suggest_integration_us(
            frame, use_dark_grid=True, target_fraction=0.70,
            candidate_integration_us=active.integration_us if active is not None else None,
            usable_bounds_nm=usable.bounds_nm if usable is not None else None,
        )
        if updated != self.integration_us:
            self.integration_us = self.device.set_integration_us(updated)
            self.exposure_changed.emit(self.integration_us)
        if updated == frame.integration_us or self.auto_remaining == 1:
            self._stop_auto()
        else:
            self.auto_remaining -= 1

    @Slot(object)
    def start_capture(self, capture):
        if self.device is None or self.capture is not None or self.dark_session is not None:
            return
        try:
            self._stop_auto()
            self.schedule = RepeatSchedule(capture.count, capture.interval_s if capture.count > 1 else 1)
            self.capture = capture
            self.capture_changed.emit(f"Saving {capture.title} · 0/{capture.count}", True)
            self.set_live(True)
        except Exception as exc:
            self.capture_changed.emit("", False)
            self.error.emit("Start capture", str(exc))

    @Slot()
    def cancel_capture(self):
        self._end_capture("Capture canceled")
        self._read_stop.clear()
        if self.running:
            self.timer.start(0)

    def _end_capture(self, message=""):
        if self.capture is not None:
            self.capture = self.schedule = None
            self.capture_changed.emit(message, False)

    @Slot(object)
    def start_dark(self, options):
        if self.device is None or self.capture is not None or self.dark_session is not None:
            return
        self._read_stop.clear()
        try:
            self._stop_auto()
            self.dark_session = self.library.start_session(
                self.device, averages=options.averages, label=options.label,
                temperature_c=options.temperature_c, temperature_source=options.temperature_source,
            )
            self.dark_requested_s = self.dark_session.remaining_integration_s
            if isinstance(self.device, DemoDevice):
                self.device.set_covered(True)
            self.dark_changed.emit("Collecting dark references", True)
            self._report_dark_progress()
            self.timer.start(0)
        except Exception as exc:
            self.error.emit("Dark session", str(exc))
            self.dark_changed.emit("", False)

    def _report_dark_progress(self):
        session = self.dark_session
        self.dark_progress.emit(DarkProgress(
            session.captures_done, session.captures_total, session.next_integration_us,
            self.dark_requested_s, session.remaining_integration_s,
        ))

    def _refresh_dark_inventory(self):
        info, wavelengths = self.device.info, self.device.wavelengths_nm
        fallback = infer_usable_bounds_nm(info, wavelengths)
        try:
            profiles = self.library.list_profiles(info, wavelengths)
        except (OSError, ValueError) as exc:
            profiles = []
            self.error.emit("List dark libraries", str(exc))
        selected_id = None
        try:
            selected_id = self.library.selected_profile_id(info, wavelengths)
        except (OSError, ValueError) as exc:
            self.dark_failure = f"Could not read dark profile selection: {exc}"
            self.error.emit("Read dark library selection", str(exc))
        active = self.library.active_profile(info, wavelengths)
        bounds = self.library.infer_usable_bounds_nm(info, wavelengths) if active is not None else fallback
        self.dark_state = DarkInventory(tuple(profiles), selected_id, active, bounds)
        self.dark_inventory.emit(self.dark_state)

    @Slot(object)
    def select_dark_library(self, profile_id):
        if self.device is None or self.capture is not None or self.dark_session is not None:
            return
        try:
            self.library.select_profile(self.device.info, self.device.wavelengths_nm, profile_id)
        except (OSError, ValueError) as exc:
            self.error.emit("Select dark library", str(exc))
            return
        self.last_frame_emit = -math.inf
        self._refresh_dark_inventory()

    @Slot()
    def _cancel_dark(self):
        if self.dark_session is not None:
            with suppress(Exception):
                self.dark_session.cancel()
            self._restore_live_exposure()
            self.dark_changed.emit("Dark session canceled", False)

    def request_immediate_dark_cancel(self):
        # DarkSession.cancel is thread-safe. Mark cancellation before a final
        # USB read returns so that a user cancellation cannot publish that run.
        session = self.dark_session
        if session is not None:
            session.cancel()

    def request_immediate_read_stop(self):
        self._read_stop.set()

    def request_immediate_close(self):
        self._closing.set()
        self.request_immediate_dark_cancel()

    def _read_cancelled(self):
        return self._closing.is_set() or self._read_stop.is_set()

    def _restore_live_exposure(self):
        self.dark_session = None
        if isinstance(self.device, DemoDevice):
            self.device.set_covered(False)
        if self.device is not None:
            try:
                self.device.set_integration_us(self.integration_us)
            except Exception as exc:
                self.set_live(False)
                self.error.emit("Restore live integration time", str(exc))

    def _tick(self):
        if self.dark_session is not None:
            self._step_dark()
        elif self.running and self.device is not None:
            if self.schedule is not None:
                until_due = self.schedule.next_due_monotonic - time.monotonic()
                nominal_s = self.integration_us * self.capture.scans_to_average / 1_000_000
                reserved = max(self.read_elapsed_s, nominal_s) + 0.02
                if 0 < until_due <= reserved:
                    self.timer.start(max(1, math.ceil(until_due * 1000)))
                    return
            self._step_live()
        if self.dark_session is not None:
            self.timer.start(0)
        elif self.running and self.device is not None:
            self.timer.start(10)

    def _step_dark(self):
        try:
            session = self.dark_session
            session.step()
            if session.completed:
                self._restore_live_exposure()
                points = session.captures_total
                self._refresh_dark_inventory()
                self.dark_changed.emit(f"Dark library saved · {points} scans", False)
            elif session.cancelled:
                self._cancel_dark()
            else:
                self._report_dark_progress()
        except Exception as exc:
            self._cancel_dark()
            self.error.emit("Dark session", str(exc))

    def _step_live(self):
        scans = self.capture.scans_to_average if self.capture is not None else self.scans_to_average
        try:
            frame = acquire_frame(
                self.device, self.integration_us, self.library, subtract_dark=self.subtract_dark,
                scans_to_average=scans, cancelled=self._read_cancelled,
            )
        except CancelledError:
            return
        except Exception as exc:
            self.set_live(False)
            self._end_capture("Capture stopped")
            self.error.emit("Read spectrum", str(exc))
            return
        if self._read_cancelled():
            return
        now = time.monotonic()
        self.read_elapsed_s = frame.acquired_monotonic - frame.started_monotonic
        if self.library.load_error != self.dark_failure:
            self.dark_failure = self.library.load_error
            if self.dark_failure:
                self.dark_failed.emit(self.dark_failure)
        if now - self.last_frame_emit >= 0.075:
            self.last_frame_emit = now
            self.frame_ready.emit(frame)
        if self.capture is not None:
            try:
                self._maybe_save(frame)
            except Exception as exc:
                self._end_capture("Capture stopped")
                self.error.emit("Save spectrum", str(exc))
        if self.auto_remaining:
            try:
                self._adjust_integration(frame)
            except Exception as exc:
                self._stop_auto()
                self.error.emit("Auto exposure", str(exc))

    def _maybe_save(self, frame):
        if not self.schedule.due(frame.started_monotonic):
            return
        capture = self.capture
        if self.subtract_dark and frame.corrected_counts is None:
            self._end_capture("Capture stopped · no matching dark")
            self.error.emit(
                "Missing dark reference",
                f"No matching dark at {format_exposure(frame.integration_us, exact=True)}. "
                "Collect a matching dark or turn off dark subtraction before saving.",
            )
            return
        sequence = self.schedule.saved + 1
        files = save_spectrum_pair(
            frame, capture.directory, capture.title, interpolate_pixels=capture.interpolate_pixels,
            run_id=self.schedule.run_id, sequence=sequence,
            scheduled_offset_s=self.schedule.scheduled_offset_s,
            scheduled_monotonic=self.schedule.next_due_monotonic,
            run_started_monotonic=self.schedule.start_monotonic,
        )
        self.schedule.record_saved(frame.started_monotonic)
        self.saved.emit(files)
        if self.schedule.done:
            self._end_capture(f"Saved {sequence} · {capture.title}")
        else:
            self.capture_changed.emit(f"Saving {capture.title} · {sequence}/{capture.count}", True)
