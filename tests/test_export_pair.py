"""Strict two-column spectrum files and their nested acquisition receipt."""

import csv
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from spectrometer_app import core
from spectrometer_app.core import DarkRecord, Frame, InstrumentInfo, interpolate_flagged_pixels, save_spectrum_pair


def spectrum():
    timestamp = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    wavelengths = np.array([812.9876543210987, 748.12, 712.3, 667.4, 585.5, 452.6, 302.7])
    raw = np.array([110, 3.123456789012345, 80, 1.625, 60, 0.125, 100])
    dark = np.array([9, 4, 10, 3, 7, 3, 1], dtype=float)
    flags = np.array([1, 0, 1, 0, -1, 0, 1], dtype=np.int8)
    return Frame(
        info=InstrumentInfo("Maya2000 Pro", "MOCK-1", "fixture", 8000, 3_000_000, 100),
        wavelengths_nm=wavelengths,
        integration_us=20_000,
        raw_counts=raw,
        started_at=timestamp,
        acquired_at=timestamp,
        started_monotonic=50.1,
        acquired_monotonic=50.2,
        dark_record=DarkRecord(20_000, dark, flags, "dark-001", 2, timestamp, "Room", 22.3, "ambient"),
        corrected_counts=raw - dark,
        dark_subtraction_requested=True,
    )


def xy_data(path: Path):
    binary = path.read_bytes()
    assert binary.startswith(b"Wavelength (nm),Amplitude\r\n")
    assert b"\n" not in binary.replace(b"\r\n", b"")
    rows = list(csv.reader(binary.decode("ascii").splitlines()))
    assert all(len(row) == 2 for row in rows)
    return np.array(rows[1:], dtype=float)


def receipt(path: Path):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    header = next(index for index, row in enumerate(rows) if row[0] == "pixel_index")
    return (
        {key[2:]: value for key, value in rows[:header]},
        [dict(zip(rows[header], row, strict=True)) for row in rows[header + 1:]],
    )


class SpectrumPairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_two_opus_style_files_preserve_exact_grid_and_signed_amplitudes(self):
        frame = spectrum()
        paths = save_spectrum_pair(
            frame, self.directory, "Café Δ", interpolate_pixels=True,
            run_id="run", sequence=2, scheduled_offset_s=10,
            scheduled_monotonic=50, run_started_monotonic=40,
        )
        self.assertEqual(set(self.directory.glob("*.csv")), {paths.raw, paths.corrected})
        self.assertEqual(paths.details.parent, self.directory / "Details")
        self.assertTrue(paths.raw.name.isascii() and paths.corrected.name.isascii())
        self.assertIn("Cafe", paths.raw.name)
        self.assertTrue(paths.corrected.name.endswith("_corrected.csv"))

        raw, corrected = xy_data(paths.raw), xy_data(paths.corrected)
        np.testing.assert_array_equal(raw[:, 0], frame.wavelengths_nm)
        np.testing.assert_array_equal(corrected[:, 0], frame.wavelengths_nm)
        np.testing.assert_array_equal(raw[:, 1], frame.raw_counts)
        displayed = interpolate_flagged_pixels(frame.corrected_counts, frame.pixel_flags)
        np.testing.assert_array_equal(np.isnan(displayed), [True, False, False, False, False, False, True])
        np.testing.assert_array_equal(
            corrected[:, 1], np.where(np.isnan(displayed), frame.corrected_counts, displayed)
        )
        self.assertLess(corrected[2, 1], 0)
        np.testing.assert_array_equal(frame.corrected_counts, frame.raw_counts - frame.dark_record.counts)

        metadata, channels = receipt(paths.details)
        self.assertEqual(metadata["title"], "Café Δ")
        self.assertEqual(metadata["dark_subtraction"], "raw_counts - dark_counts")
        self.assertEqual(metadata["dark_temperature_c"], "22.3")
        self.assertEqual(metadata["interpolated_channels"], "2")
        self.assertEqual(metadata["unbracketed_channels_retained"], "2")
        self.assertEqual(metadata["raw_saturated_pixels"], "2")
        self.assertEqual(metadata["run_id"], "run")
        self.assertEqual(metadata["sequence"], "2")
        self.assertAlmostEqual(float(metadata["read_start_offset_from_run_s"]), 10.1)
        self.assertAlmostEqual(float(metadata["read_start_delay_from_due_s"]), 0.1)
        self.assertEqual(metadata["raw_file"], paths.raw.name)
        self.assertEqual(metadata["corrected_file"], paths.corrected.name)
        self.assertEqual(channels[0]["pixel_interpolation"], "retained_unbracketed")
        self.assertEqual(channels[0]["raw_saturated"], "1")
        self.assertEqual(channels[0]["pixel_flag"], "hot")
        self.assertEqual(float(channels[0]["dark_counts"]), 9)
        self.assertEqual(channels[2]["pixel_interpolation"], "interpolated")
        np.testing.assert_array_equal([float(row["corrected_export_counts"]) for row in channels], corrected[:, 1])

    def test_identity_companion_is_labeled_and_prior_captures_are_immutable(self):
        frame = replace(spectrum(), dark_record=None, corrected_counts=None, dark_subtraction_requested=False)
        first = save_spectrum_pair(frame, self.directory, "Unchanged", interpolate_pixels=True)
        before = {path: path.read_bytes() for path in (first.raw, first.corrected, first.details)}
        second = save_spectrum_pair(frame, self.directory, "Unchanged", interpolate_pixels=True)
        self.assertNotEqual(first, second)
        self.assertEqual(first.raw.read_bytes(), first.corrected.read_bytes())
        self.assertTrue(first.corrected.name.endswith("_corrected_identity.csv"))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        metadata, channels = receipt(first.details)
        self.assertEqual(metadata["corrected_companion"], "identity, same measured counts")
        self.assertEqual(metadata["pixel_interpolation_requested"], "True")
        self.assertEqual(metadata["pixel_interpolation"], "none")
        self.assertEqual(channels[0]["dark_counts"], "")

    def test_interpolation_can_be_saved_without_dark_subtraction(self):
        frame = replace(spectrum(), corrected_counts=None, dark_subtraction_requested=False)
        paths = save_spectrum_pair(frame, self.directory, "Flags only", interpolate_pixels=True)
        self.assertTrue(paths.corrected.name.endswith("_corrected.csv"))
        metadata, channels = receipt(paths.details)
        self.assertEqual(metadata["dark_subtraction"], "off")
        self.assertEqual(channels[2]["dark_counts"], "10.0")
        self.assertEqual(channels[2]["dark_corrected_counts"], "")
        expected = (frame.raw_counts[1] + frame.raw_counts[3]) / 2
        self.assertEqual(xy_data(paths.corrected)[2, 1], expected)

    def test_invalid_frames_are_rejected_before_writing(self):
        frame = spectrum()
        cases = (
            (replace(frame, dark_record=None, corrected_counts=None), "matching dark"),
            (replace(frame, wavelengths_nm=np.ones(7)), "distinct wavelength coordinates"),
            (replace(frame, wavelengths_nm=np.ones(5)), "equal lengths"),
            (replace(frame, raw_counts=np.full(7, np.inf)), "finite"),
            (replace(frame, corrected_counts=frame.corrected_counts + 1), "differs"),
        )
        for invalid, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    save_spectrum_pair(invalid, self.directory, "Invalid")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_opus_path_budget_compacts_title_and_rejects_an_overlong_directory(self):
        abbreviated = self.directory / ("A" * 65)
        paths = save_spectrum_pair(spectrum(), abbreviated, "long title" * 25)
        self.assertLessEqual(max(len(str(paths.raw.absolute())), len(str(paths.corrected.absolute()))), 220)
        self.assertEqual(receipt(paths.details)[0]["title"], "long title" * 25)
        overlong = self.directory / ("A" * 180)
        with self.assertRaisesRegex(OSError, "shorter output folder"):
            save_spectrum_pair(spectrum(), overlong, "No space")
        self.assertFalse(overlong.exists())

    def test_publication_collision_rolls_back_only_new_files(self):
        publish = core._new_file
        existing = []

        def collision(source, destination):
            if destination.name.endswith("_corrected.csv"):
                destination.write_bytes(b"existing spectrum")
                existing.append(destination)
            return publish(source, destination)

        with patch.object(core, "_new_file", side_effect=collision):
            with self.assertRaises(FileExistsError):
                save_spectrum_pair(spectrum(), self.directory, "Collision")
        self.assertEqual(list(self.directory.glob("*.csv")), existing)
        self.assertEqual(existing[0].read_bytes(), b"existing spectrum")
        self.assertEqual(list((self.directory / "Details").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
