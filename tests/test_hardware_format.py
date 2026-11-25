"""Regression checks for the two device-specific SeaBreeze workarounds."""

from types import SimpleNamespace

import numpy as np
import pytest

from spectrometer_app.hardware import _decode_usb650, _read_wavelengths


def test_usb650_aligned_frame_preserves_detector_order():
    expected = (np.arange(2048, dtype=np.uint16) * 19) % 4096
    samples = expected.reshape(32, 64)
    blocks = np.column_stack((samples & 0xFF, (samples >> 8) | 0xA0)).astype(np.uint8)
    raw = blocks.tobytes() + b"\x69"
    np.testing.assert_array_equal(_decode_usb650(raw), expected)
    for invalid in (raw[:-1], raw[:-1] + b"\x00"):
        with pytest.raises(RuntimeError, match="Invalid USB650 frame"):
            _decode_usb650(invalid)


def test_maya_wavelength_coefficient_can_fill_all_15_bytes():
    encoded = (b"199.996597", b"0.46", b"-0.000001", b"-8.26350996E-11")

    class CalibrationProtocol:
        def send(self, command, slot):
            assert command == 0x05
            self.slot = slot

        def receive(self, *, size, mode):
            assert size == 64 and mode == "low_speed"
            return bytes((0x05, self.slot)) + encoded[self.slot - 1].ljust(15, b"\x00")

    wavelength = _read_wavelengths(SimpleNamespace(protocol=CalibrationProtocol(), _spectrum_length=2304))
    pixels = np.arange(2304, dtype=float)
    expected = 199.996597 + 0.46 * pixels - 1e-6 * pixels ** 2 - 8.26350996e-11 * pixels ** 3
    np.testing.assert_allclose(wavelength, expected)
