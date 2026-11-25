import csv
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from spectrometer_app.core import (
    DarkLibrary,
    InstrumentInfo,
    RepeatSchedule,
    acquire_frame,
    detect_dark_pixels,
    infer_usable_bounds_nm,
    save_spectrum,
    suggest_integration_us,
)


class FakeDevice:
    def __init__(self):
        self.info = InstrumentInfo("Maya2000Pro", "UNIT-1", "test", 8_000, 42_000, 65535)
        self.wavelengths_nm = np.linspace(500.0, 400.0, 20)
        self.integration_us = None
        self.illuminated = False
        self.after_read = lambda: None

    def set_integration_us(self, integration_us):
        self.integration_us = integration_us
        return integration_us

    def read_counts(self):
        counts = np.full(self.wavelengths_nm.size, 100.0 + self.integration_us / 1000)
        counts[6] += 60
        counts[15] -= 50
        if self.illuminated:
            counts[10] += 100.5
            counts[11] -= 5
        self.after_read()
        return counts

    def close(self):
        pass


class CoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.library = DarkLibrary(self.directory / "dark")
        self.device = FakeDevice()

    def dark_sweep(self, averages=1, **tags):
        session = self.library.start_session(self.device, averages, **tags)
        while session.next_integration_us is not None:
            session.step()
        return session

    def test_completed_dark_profile_matches_exact_configuration(self):
        session = self.dark_sweep(averages=2)
        self.assertTrue(session.completed)
        self.assertEqual((session.captures_done, session.captures_total), (8, 8))
        self.assertEqual(session.skipped_integration_us[0], 50_000)
        self.assertTrue(session.published_path.is_file())
        reloaded = DarkLibrary(self.library.directory)
        self.device.illuminated = True
        raw = acquire_frame(self.device, 20_000, reloaded)
        corrected = acquire_frame(self.device, 20_000, reloaded, subtract_dark=True)
        self.assertIsNone(raw.corrected_counts)
        self.assertEqual((raw.pixel_flags[6], raw.pixel_flags[15]), (1, -1))
        self.assertEqual(corrected.dark_record.averages, 2)
        np.testing.assert_array_equal(raw.raw_counts, corrected.raw_counts)
        np.testing.assert_array_equal(corrected.wavelengths_nm, self.device.wavelengths_nm)
        self.assertEqual(corrected.corrected_counts[10], 100.5)
        self.assertEqual(corrected.corrected_counts[11], -5)
        self.assertFalse(corrected.raw_counts.flags.writeable)
        self.assertIsNone(reloaded.find(self.device.info, self.device.wavelengths_nm, 21_000))
        self.assertIsNone(reloaded.find(self.device.info, self.device.wavelengths_nm[::-1], 20_000))
        for key, value in (("serial", "WRONG"), ("backend", "other"), ("readout_mode", "changed")):
            other_instrument = replace(self.device.info, **{key: value})
            self.assertIsNone(reloaded.find(other_instrument, self.device.wavelengths_nm, 20_000))

    def test_cancel_during_last_native_read_retains_previous_dark(self):
        previous = self.dark_sweep()
        session = self.library.start_session(self.device)
        for _ in range(3):
            session.step()
        self.device.after_read = session.cancel
        self.assertIsNone(session.step())
        self.assertTrue(session.cancelled)
        self.assertFalse(session.completed)
        self.assertEqual(list(previous.published_path.parent.glob("*.npz")), [previous.published_path])
        current = self.library.find(self.device.info, self.device.wavelengths_nm, 40_000)
        self.assertEqual(current.profile_id, previous.published_path.stem)

    def test_new_session_preserves_previous_result_and_selects_new_profile(self):
        previous = self.dark_sweep()
        self.library.find(self.device.info, self.device.wavelengths_nm, 20_000)
        new = self.dark_sweep()
        self.assertNotEqual(previous.published_path, new.published_path)
        self.assertTrue(previous.published_path.is_file())
        current = self.library.find(self.device.info, self.device.wavelengths_nm, 20_000)
        self.assertEqual(current.profile_id, new.published_path.stem)

    def test_inventory_and_pin_survive_restart_without_altering_existing_profile(self):
        old = self.dark_sweep()
        original = old.published_path.read_bytes()
        new = self.dark_sweep(averages=2, label=" Commissioning ", temperature_c=22.3, temperature_source="ambient")
        info, wavelengths = self.device.info, self.device.wavelengths_nm
        self.assertEqual(self.library.active_profile(info, wavelengths).profile_id, new.published_path.stem)
        summaries = self.library.list_profiles(info, wavelengths)
        self.assertEqual([profile.profile_id for profile in summaries], [new.published_path.stem, old.published_path.stem])
        self.assertEqual(summaries[0].integration_us, (10_000, 20_000, 30_000, 40_000))
        self.assertEqual(summaries[0].label, "Commissioning")
        self.assertEqual(summaries[0].temperature_c, 22.3)
        self.assertEqual(summaries[0].temperature_source, "ambient")
        self.assertIsNone(summaries[1].temperature_c)
        self.library.select_profile(info, wavelengths, old.published_path.stem)
        restarted = DarkLibrary(self.library.directory)
        self.assertEqual(restarted.selected_profile_id(info, wavelengths), old.published_path.stem)
        self.assertEqual(restarted.find(info, wavelengths, 30_000).profile_id, old.published_path.stem)
        with self.assertRaises(ValueError):
            restarted.select_profile(replace(info, serial="other"), wavelengths, old.published_path.stem)
        third = self.dark_sweep()
        self.assertEqual(restarted.active_profile(info, wavelengths).profile_id, old.published_path.stem)
        self.assertEqual(old.published_path.read_bytes(), original)
        restarted.select_profile(info, wavelengths, None)
        self.assertIsNone(restarted.selected_profile_id(info, wavelengths))
        self.assertEqual(restarted.active_profile(info, wavelengths).profile_id, third.published_path.stem)

    def test_missing_pinned_profile_does_not_substitute_another_dark(self):
        pinned = self.dark_sweep()
        self.dark_sweep()
        info, wavelengths = self.device.info, self.device.wavelengths_nm
        self.library.select_profile(info, wavelengths, pinned.published_path.stem)
        pinned.published_path.unlink()
        restarted = DarkLibrary(self.library.directory)
        self.assertIsNone(restarted.find(info, wavelengths, 30_000))
        self.assertIn(pinned.published_path.name, restarted.load_error)

    def test_temperature_value_requires_a_manual_measurement_source(self):
        for tags in (
            {"temperature_c": 21.1},
            {"temperature_source": "instrument"},
            {"temperature_c": 21.1, "temperature_source": "assumed"},
        ):
            with self.assertRaises(ValueError):
                self.library.start_session(self.device, **tags)

    def test_failed_dark_sweep_has_no_artifact(self):
        session = self.library.start_session(self.device)
        session.step()
        self.device.wavelengths_nm = self.device.wavelengths_nm + 1
        with self.assertRaisesRegex(RuntimeError, "changed during"):
            session.step()
        self.assertTrue(session.failed)
        self.assertIsNone(session.step())
        self.assertFalse(self.library.directory.exists())

    def test_corrupt_dark_returns_unavailable(self):
        path = self.dark_sweep().published_path
        path.write_bytes(b"broken npz file")
        reloaded = DarkLibrary(self.library.directory)
        self.assertIsNone(reloaded.find(self.device.info, self.device.wavelengths_nm, 20_000))
        self.assertIn(path.name, reloaded.load_error)

    def test_save_spectrum_keeps_signed_correction_detector_order_and_provenance(self):
        self.dark_sweep()
        self.device.illuminated = True
        frame = acquire_frame(self.device, 20_000, self.library, subtract_dark=True)
        path = save_spectrum(frame, self.directory, "Control, A", run_id="sample", sequence=1)
        second = save_spectrum(frame, self.directory, "Control, A", run_id="sample", sequence=1)
        self.assertNotEqual(path, second)
        with path.open(encoding="utf-8", newline="") as file:
            rows = list(csv.reader(file))
        split = next(index for index, row in enumerate(rows) if row[0] == "pixel_index")
        metadata = dict(rows[:split])
        data = rows[split + 1:]
        self.assertEqual(metadata["# title"], "Control, A")
        self.assertEqual(metadata["# run_id"], "sample")
        self.assertEqual(metadata["# dark_subtraction"], "raw_counts - dark_counts")
        self.assertTrue(metadata["# read_started_utc"].endswith("Z"))
        self.assertGreater(float(data[0][1]), float(data[-1][1]))
        self.assertEqual(float(data[11][4]), -5.0)
        self.assertEqual(data[6][-1], "hot")
        self.assertEqual(data[15][-1], "cold")

    def test_exposure_suggestion_uses_narrow_peak_and_dark_grid(self):
        self.dark_sweep()
        frame = acquire_frame(self.device, 20_000, self.library)
        counts = frame.raw_counts.copy()
        counts[6] = 65_535  # flagged hot in matched dark
        counts[10] = 61_000  # sharp valid peak
        frame = replace(frame, raw_counts=counts)
        self.assertLess(suggest_integration_us(frame), 20_000)
        self.assertEqual(suggest_integration_us(frame, use_dark_grid=True), 20_000)

    def test_auto_exposure_uses_bounds_and_eligible_dark_times(self):
        frame = acquire_frame(self.device, 20_000)
        counts = frame.raw_counts.copy()
        counts[0] = 65_535
        counts[9] = 25_000
        frame = replace(frame, raw_counts=counts)
        bounds = tuple(sorted(frame.wavelengths_nm[[3, 16]]))
        suggestion = suggest_integration_us(
            frame, usable_bounds_nm=bounds, candidate_integration_us=(10_000, 30_000, 80_000)
        )
        self.assertEqual(suggestion, 30_000)
        self.assertEqual(frame.raw_counts[0], 65_535)

    def test_maya_and_generic_bounds_respect_native_grid_and_defensible_padding(self):
        maya = replace(self.device.info, backend="pyseabreeze 2.11.0")
        reversed_grid = np.linspace(1220.0, 200.0, 2304)
        bounds = infer_usable_bounds_nm(maya, reversed_grid)
        self.assertEqual(bounds.bounds_nm, (reversed_grid[2057], reversed_grid[10]))
        self.assertEqual(bounds.source, "documented detector layout")
        self.assertIsNone(infer_usable_bounds_nm(replace(maya, backend="demo"), reversed_grid))
        wavelengths = np.linspace(400.0, 550.0, 100)
        stable = np.full(100, 60.0)
        stable[-20:] = 0
        stable[:5] = 1500
        bounds = infer_usable_bounds_nm(self.device.info, wavelengths, stable)
        self.assertEqual(bounds.bounds_nm, (wavelengths[0], wavelengths[79]))
        self.assertIsNone(infer_usable_bounds_nm(self.device.info, wavelengths, np.zeros(100)))
        spectra = np.linspace(70.0, 130.0, 15)[:, None] * np.ones(100)
        spectra[:, :18] = 12
        exposures = np.arange(10_000, 151_000, 10_000)
        bounds = infer_usable_bounds_nm(self.device.info, wavelengths, spectra, exposures)
        self.assertEqual(bounds.bounds_nm, (wavelengths[18], wavelengths[-1]))
        self.assertEqual(bounds.source, "dark library")

    def test_smooth_dark_gradient_does_not_flag_detector_edges(self):
        counts = np.linspace(300.0, 2500.0, 50)
        np.testing.assert_array_equal(detect_dark_pixels(counts), np.zeros(counts.size))

    def test_repeat_schedule_rejects_old_frames_and_catch_up_bursts(self):
        schedule = RepeatSchedule(3, 10.0, start_monotonic=100.0)
        for early, actual in ((99.9, 100.0), (109.9, 123.0), (132.9, 133.0)):
            self.assertFalse(schedule.done)
            self.assertFalse(schedule.due(early))
            with self.assertRaises(ValueError):
                schedule.record_saved(early)
            schedule.record_saved(actual)
        self.assertTrue(schedule.done)
        self.assertIsNone(schedule.next_due_monotonic)


if __name__ == "__main__":
    unittest.main()
