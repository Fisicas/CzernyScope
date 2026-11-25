"""Average complete raw scan batches without losing detector clipping provenance."""

import csv
import tempfile
import unittest
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import numpy as np

from spectrometer_app.core import (
    MAX_SCANS_TO_AVERAGE,
    DarkRecord,
    InstrumentInfo,
    acquire_frame,
    save_spectrum_pair,
    suggest_integration_us,
)


SCAN_ROWS = np.array([[100.0, 10, 5, 4], [20, 90, 25, 7], [20, 90, 25, 9]])


class SequenceDevice:
    def __init__(self, rows=SCAN_ROWS):
        self.info = InstrumentInfo("Maya2000 Pro", "fixture", "fixture", 8_000, 3_000_000, 100)
        self.wavelengths_nm = np.array([750.0, 680, 490, 320])
        self._rows = rows
        self._buffer = np.empty(self.wavelengths_nm.size)
        self.settings = []
        self.read_count = 0

    def set_integration_us(self, value):
        self.settings.append(value)
        return value

    def read_counts(self):
        data = np.asarray(self._rows[self.read_count])
        self.read_count += 1
        if data.shape != self._buffer.shape:
            return data
        self._buffer[:] = data
        return self._buffer


class AveragingTests(unittest.TestCase):
    def test_default_is_one_unmodified_detector_read(self):
        device = SequenceDevice()
        frame = acquire_frame(device, 20_000)
        self.assertEqual(device.settings, [20_000])
        self.assertEqual(device.read_count, 1)
        self.assertEqual(frame.scans_to_average, 1)
        self.assertIsNone(frame.constituent_counts)
        np.testing.assert_array_equal(frame.raw_counts, SCAN_ROWS[0])

    def test_average_preserves_sources_and_applies_one_matched_signed_dark(self):
        device = SequenceDevice()
        dark = DarkRecord(
            20_000, np.array([80.0, 30, 25, 10]), np.zeros(4, dtype=np.int8),
            "matched", 2, datetime.now(timezone.utc),
        )
        library = Mock()
        library.find.return_value = dark
        frame = acquire_frame(device, 20_000, library, subtract_dark=True, scans_to_average=3)
        expected = np.mean(SCAN_ROWS, axis=0)
        self.assertEqual(device.settings, [20_000])
        self.assertEqual(device.read_count, 3)
        self.assertEqual(frame.scans_to_average, 3)
        library.find.assert_called_once()
        self.assertEqual(library.find.call_args.args[-1], 20_000)
        np.testing.assert_array_equal(frame.constituent_counts, SCAN_ROWS)
        np.testing.assert_array_equal(frame.wavelengths_nm, device.wavelengths_nm)
        np.testing.assert_array_equal(frame.raw_counts, expected)
        np.testing.assert_array_equal(frame.corrected_counts, expected - dark.counts)
        self.assertLess(frame.corrected_counts[0], 0)
        self.assertFalse(frame.raw_counts.flags.writeable)
        self.assertFalse(frame.constituent_counts.flags.writeable)
        self.assertGreaterEqual(frame.acquired_monotonic, frame.started_monotonic)
        self.assertEqual(frame.started_at.tzinfo, timezone.utc)
        self.assertEqual(frame.acquired_at.tzinfo, timezone.utc)

    def test_batch_stops_before_or_after_first_read_on_request(self):
        before_first = SequenceDevice()
        with self.assertRaises(CancelledError):
            acquire_frame(before_first, 20_000, scans_to_average=3, cancelled=lambda: True)
        self.assertEqual(before_first.read_count, 0)
        during_first = SequenceDevice()
        library = Mock()
        with self.assertRaises(CancelledError):
            acquire_frame(
                during_first, 20_000, library, scans_to_average=3,
                cancelled=lambda: during_first.read_count >= 1,
            )
        self.assertEqual(during_first.read_count, 1)
        library.find.assert_not_called()

    def test_bad_scan_count_and_inconsistent_scan_lengths_are_rejected(self):
        device = SequenceDevice()
        for invalid in (0, True, 1.5, MAX_SCANS_TO_AVERAGE + 1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "Scans to average"):
                    acquire_frame(device, 20_000, scans_to_average=invalid)
        self.assertEqual(device.settings, [])
        bad = SequenceDevice([SCAN_ROWS[0], [1, 2]])
        with self.assertRaisesRegex(ValueError, "Detector scan 2 length"):
            acquire_frame(bad, 20_000, scans_to_average=2)

    def test_clipping_that_the_mean_hides_affects_saturation_and_auto_exposure(self):
        frame = acquire_frame(SequenceDevice(), 20_000, scans_to_average=3)
        self.assertLess(np.max(frame.raw_counts), 100)
        np.testing.assert_array_equal(frame.saturation_mask, [True, False, False, False])
        np.testing.assert_array_equal(frame.saturated_scan_counts, [1, 0, 0, 0])
        self.assertEqual(frame.saturated_pixels, 1)
        self.assertEqual(frame.saturated_scans, 1)
        self.assertEqual(suggest_integration_us(frame), 10_000)
        self.assertGreater(suggest_integration_us(frame, usable_bounds_nm=(320, 700)), 20_000)

    def test_two_column_pair_and_details_identify_the_average_and_all_inputs(self):
        frame = acquire_frame(SequenceDevice(), 20_000, scans_to_average=3)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            paths = save_spectrum_pair(frame, directory, "Averaged spectrum")
            with paths.raw.open(encoding="ascii", newline="") as stream:
                rows = list(csv.reader(stream))
            with paths.details.open(encoding="utf-8", newline="") as stream:
                details = list(csv.reader(stream))
        self.assertEqual(rows[0], ["Wavelength (nm)", "Amplitude"])
        self.assertTrue(all(len(row) == 2 for row in rows))
        np.testing.assert_array_equal([float(row[1]) for row in rows[1:]], frame.raw_counts)
        header = next(index for index, row in enumerate(details) if row[0] == "pixel_index")
        metadata = {key[2:]: value for key, value in details[:header]}
        channels = [dict(zip(details[header], row, strict=True)) for row in details[header + 1:]]
        self.assertEqual(metadata["scans_to_average"], "3")
        self.assertIn("unsmoothed arithmetic mean", metadata["raw_counts"])
        self.assertTrue(metadata["read_started_utc"].endswith("Z"))
        self.assertTrue(metadata["read_finished_utc"].endswith("Z"))
        self.assertEqual(metadata["raw_saturated_scans"], "1")
        self.assertEqual(channels[0]["raw_saturated"], "1")
        self.assertEqual(channels[0]["raw_saturated_scan_count"], "1")
        read_back = [
            [float(row[f"raw_scan_{scan:03d}_counts"]) for row in channels]
            for scan in range(1, frame.scans_to_average + 1)
        ]
        np.testing.assert_array_equal(read_back, SCAN_ROWS)


if __name__ == "__main__":
    unittest.main()
