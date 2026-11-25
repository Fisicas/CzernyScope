"""Human-entered quantity aliases, precision and invalid-input checks."""

import unittest

from spectrometer_app.quantities import format_exposure, format_number, format_quantity, parse_quantity


class QuantityTests(unittest.TestCase):
    def test_time_inputs_with_default_units_and_equivalent_notation(self):
        for text in ("100", "100 ms", "100000 us", "100,000 µs", "100_000 μs", "0.1 s", "1e−1 seconds"):
            with self.subTest(text=text):
                self.assertEqual(parse_quantity(text, "ms"), 100)
        self.assertEqual(parse_quantity("  .5 minutes  ", "s"), 30)
        self.assertEqual(parse_quantity("250 milliseconds", "s"), 0.25)
        self.assertEqual(parse_quantity("1_234.5_6 ms", "ms"), 1234.56)
        self.assertEqual(parse_quantity("1,234.56 ms", "ms"), 1234.56)

    def test_wavelength_temperature_and_discrete_dimension_suffixes(self):
        for text in ("635", "635 nm", ".635 µm", "0.635 μm", "6.35e3 Å", "6350 Å"):
            with self.subTest(text=text):
                self.assertEqual(parse_quantity(text, "nm"), 635)
        self.assertEqual(parse_quantity("−40 C", "°C"), -40)
        self.assertEqual(parse_quantity("68 ° F", "°C"), 20)
        self.assertEqual(parse_quantity("293.15 K", "°C"), 20)
        self.assertEqual(parse_quantity("32°F", "°C"), 0)
        self.assertEqual(parse_quantity("2.1e1", "°C"), 21)
        self.assertEqual(parse_quantity("1,234 spectra", "spectra"), 1234)
        self.assertEqual(parse_quantity("1 spectrum", "spectra"), 1)
        self.assertEqual(parse_quantity("1 scan", "scans"), 1)

    def test_malformed_nonfinite_and_dimension_mismatches_are_rejected(self):
        invalid = (
            ("", "ms"), ("NaN", "nm"), ("∞ s", "s"), ("1e309", "s"),
            ("1e-1_000_000_000 ms", "ms"), ("1e-", "ms"), ("100;", "ms"),
            ("1000,1", "ms"), ("1,0_0", "ms"), ("1__2", "ms"), ("1_.0", "ms"),
            ("1,200, 000", "ms"), ("100 nm", "ms"), ("2 cameras", "spectra"),
            ("1 scans", "spectra"), ("2 ms", "nm"), ("4 C", "s"),
        )
        for text, unit in invalid:
            with self.subTest(text=text, unit=unit):
                with self.assertRaises(ValueError):
                    parse_quantity(text, unit)

    def test_display_format_groups_trims_and_normalizes_rounded_negative_zero(self):
        self.assertEqual(format_number(1234.5678), "1,234.57")
        self.assertEqual(format_number(-1.005), "-1.01")
        self.assertEqual(format_number(6_001.004), "6,001")
        self.assertEqual(format_number(-0.0049), "0")
        self.assertEqual(format_number(1234.5678, decimals=3), "1,234.568")
        self.assertEqual(format_quantity(23.40, "°C"), "23.4 °C")
        self.assertEqual(format_quantity(23.40, ""), "23.4")

    def test_exposure_readout_can_round_trip_exact_off_grid_settings(self):
        cases = (
            (1, "1 µs", "1 µs"), (10, "10 µs", "10 µs"), (999, "999 µs", "999 µs"),
            (1000, "1 ms", "1 ms"), (1001, "1 ms", "1,001 µs"),
            (100_001, "100 ms", "100,001 µs"), (100_005, "100.01 ms", "100,005 µs"),
            (100_010, "100.01 ms", "100.01 ms"),
            (3_000_001, "3,000 ms", "3,000,001 µs"),
        )
        for microseconds, ordinary, exact in cases:
            with self.subTest(microseconds=microseconds):
                self.assertEqual(format_exposure(microseconds), ordinary)
                self.assertEqual(format_exposure(microseconds, exact=True), exact)
                self.assertEqual(round(parse_quantity(exact, "ms") * 1000), microseconds)


if __name__ == "__main__":
    unittest.main()
