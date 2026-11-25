"""USB spectrometer adapter using the open-source SeaBreeze Python backend.

The USB650 aligned-frame workaround and the Maya2000 Pro integration floor
are drawn from LightKeeper (MIT). See the notice at the end of this file.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .core import InstrumentInfo


_VENDOR_ID = 0x2457
_USB650 = 0x1014
_MAYA2000_PRO = 0x102A
_MODELS = {_MAYA2000_PRO: "Maya2000 Pro", _USB650: "USB650"}


@dataclass(frozen=True)
class DeviceDescriptor:
    model: str
    serial: str
    backend: str
    identifier: str

    @property
    def label(self) -> str:
        return f"{self.model} · {self.serial or self.identifier}"


class Instrument(Protocol):
    info: InstrumentInfo
    wavelengths_nm: np.ndarray

    def set_integration_us(self, value: int) -> int: ...

    def read_counts(self) -> np.ndarray: ...

    def close(self) -> None: ...


@cache
def _usb_backend():
    # Prefer the current environment, then project-installed dependencies or
    # the standalone dependency cache from a local LightKeeper installation.
    profile_app_data = Path.home() / "AppData" / "Local"
    local_app_data = Path(os.environ.get("LOCALAPPDATA", profile_app_data))
    dependencies = (
        Path(__file__).resolve().parent.parent / ".spectrometer-deps",
        local_app_data / "LightKeeper" / "deps",
        profile_app_data / "LightKeeper" / "deps",
    )
    for folder in dependencies:
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.append(str(folder))

    try:
        import libusb_package
        import seabreeze
        from seabreeze.pyseabreeze.devices import SeaBreezeDevice
        from seabreeze.pyseabreeze.transport import USBTransportHandle
    except ImportError as exc:
        raise RuntimeError(
            "SeaBreeze USB dependencies could not be loaded. See the hardware setup instructions."
        ) from exc

    backend = libusb_package.get_libusb1_backend()
    if backend is None:
        raise RuntimeError("libusb could not load a native library for this Python architecture.")
    return backend, SeaBreezeDevice, USBTransportHandle, f"pyseabreeze {seabreeze.__version__}"


def _find_usb_devices():
    backend, _, _, _ = _usb_backend()
    import usb.core

    return usb.core.find(
        find_all=True,
        idVendor=_VENDOR_ID,
        custom_match=lambda device: device.idProduct in _MODELS,
        backend=backend,
    )


def _usb_location(device) -> str:
    return f"USB {device.bus}:{device.address}"


def list_devices() -> list[DeviceDescriptor]:
    """List the two supported USB models without opening their interfaces."""
    _, _, _, backend_name = _usb_backend()
    devices = [
        DeviceDescriptor(_MODELS[device.idProduct], "", backend_name, _usb_location(device))
        for device in _find_usb_devices()
    ]
    return sorted(devices, key=lambda device: (device.model, device.identifier))


def _drain_pending_usb_input(device) -> None:
    """Discard incomplete old replies from earlier USB sessions before sending commands."""
    import usb.core
    import usb.util

    interface_number = None
    try:
        try:
            interface = device.get_active_configuration()[(0, 0)]
        except usb.core.USBError as exc:
            if str(exc) == "Configuration not set":
                return
            raise
        usb.util.claim_interface(device, interface.bInterfaceNumber)
        interface_number = interface.bInterfaceNumber
        for endpoint in interface:
            if usb.util.endpoint_direction(endpoint.bEndpointAddress) != usb.util.ENDPOINT_IN:
                continue
            for _ in range(32):
                try:
                    endpoint.read(16384, timeout=25)
                except usb.core.USBTimeoutError:
                    break
            else:
                raise RuntimeError(f"USB endpoint {endpoint.bEndpointAddress:#04x} did not become idle.")
    finally:
        if interface_number is not None:
            with suppress(usb.core.USBError):
                usb.util.release_interface(device, interface_number)
        with suppress(usb.core.USBError):
            usb.util.dispose_resources(device)


def open_device(descriptor: DeviceDescriptor) -> Instrument:
    """Open the selected USB interface and read the instrument's calibration and serial."""
    _, device_class, handle_class, backend_name = _usb_backend()
    for usb_device in _find_usb_devices():
        model = _MODELS[usb_device.idProduct]
        if model != descriptor.model or _usb_location(usb_device) != descriptor.identifier:
            continue
        try:
            _drain_pending_usb_input(usb_device)
            device = device_class(handle_class(usb_device))
            return _SeaBreezeInstrument(device, usb_device, model, backend_name)
        except Exception as exc:
            raise RuntimeError(
                f"Could not open {descriptor.label}. Close other applications using the spectrometer "
                f"and verify its WinUSB driver. USB error: {exc}"
            ) from exc
    raise RuntimeError(f"{descriptor.label} is no longer connected. Scan again to refresh the list.")


class _SeaBreezeInstrument:
    def __init__(self, device: Any, usb_device: Any, model: str, backend_name: str):
        self._device = device
        self._usb_device = usb_device
        self._integration_us: int | None = None
        self._is_usb650 = model == _MODELS[_USB650]
        try:
            device.open()
            self._feature = device.f.spectrometer
            serial = device.serial_number.strip()
            if not serial or serial == "?":
                raise RuntimeError("The device did not report its serial number.")
            self.wavelengths_nm = _read_wavelengths(self._feature)
            if self.wavelengths_nm.ndim != 1 or self.wavelengths_nm.size < 2:
                raise RuntimeError("The instrument did not return a usable wavelength calibration.")
            if not np.all(np.isfinite(self.wavelengths_nm)):
                raise RuntimeError("The wavelength calibration contains non-finite values.")

            minimum, upper_bound = self._feature.get_integration_time_micros_limits()
            step = 1000 if self._is_usb650 else 1
            minimum = minimum if self._is_usb650 else max(8000, minimum)
            # The pyseabreeze OOI driver rejects its upper limit itself.
            maximum = minimum + ((upper_bound - 1 - minimum) // step) * step
            if self._is_usb650:
                maximum = min(maximum, 60_000_000)
            if self._is_usb650:
                readout_mode = (
                    "USB650 native 12-bit counts from packet-aligned USB reads. "
                    "Two settling frames discarded after integration changes."
                )
            else:
                scale = float(self._feature._normalization_value)
                readout_mode = (
                    f"Maya2000 Pro detector counts times SeaBreeze saturation scale {scale!r}. "
                    "One settling frame discarded after integration changes."
                )
            readout_mode += " No electric-dark or nonlinearity correction."
            self.info = InstrumentInfo(
                model, serial, backend_name, minimum, maximum,
                self._feature.get_maximum_intensity(), step, readout_mode,
            )
            self._feature.set_trigger_mode(0)
        except Exception:
            with suppress(Exception):
                self.close()
            raise

    def set_integration_us(self, value: int) -> int:
        """Program integration time, using integer microseconds and the device's grid."""
        value = int(value)
        minimum = self.info.min_integration_us
        if not minimum <= value <= self.info.max_integration_us:
            raise ValueError(f"Unsupported integration time: {value} µs")
        value -= (value - minimum) % self.info.integration_step_us
        if value == self._integration_us:
            return value

        previous = self._integration_us or value
        self._integration_us = None
        self._feature.set_integration_time_micros(value)
        # The Maya can return a frame from the previous exposure. On the
        # USB650 retain the two-frame settling rule used in LightKeeper.
        # Settling here keeps the discarded frames out of acquisition timing.
        original_timeout = self._usb_device.default_timeout
        self._usb_device.default_timeout += max(0, previous - value) // 1000
        try:
            self._read_frame(max(previous, value))
        finally:
            self._usb_device.default_timeout = original_timeout
        if self._is_usb650:
            self._read_frame(value)
        self._integration_us = value
        return value

    def read_counts(self) -> np.ndarray:
        if self._integration_us is None:
            raise RuntimeError("Set the integration time before acquiring a spectrum.")
        return self._read_frame(self._integration_us)

    def _read_frame(self, integration_us: int) -> np.ndarray:
        if self._is_usb650:
            protocol = self._feature.protocol
            protocol.send(0x09)
            # A packet-aligned read avoids leaving the short trailer queued
            # on Windows. The packet framing differs from the regular OOI read.
            raw = protocol.transport.read(
                size=8192, timeout_ms=integration_us // 1000 + 2000, mode="high_speed"
            )
            counts = _decode_usb650(raw)
        else:
            # SeaBreeze provides detector counts with its factory saturation
            # scaling. No electric-dark or nonlinearity correction is applied.
            counts = np.asarray(self._feature.get_intensities(), dtype=np.float64)
        if counts.shape != self.wavelengths_nm.shape:
            raise RuntimeError("The spectrum length differs from its wavelength calibration.")
        return counts

    def close(self) -> None:
        self._device.close()


def _read_wavelengths(feature) -> np.ndarray:
    """Read the four ASCII factory coefficients in the native pixel order."""
    protocol = feature.protocol
    coefficients = []
    for slot in range(1, 5):
        protocol.send(0x05, slot)
        reply = protocol.receive(size=64, mode="low_speed")
        if len(reply) != 17 or reply[:2] != bytes((0x05, slot)):
            raise RuntimeError(f"Invalid wavelength calibration response for EEPROM slot {slot}.")
        # SeaBreeze 2.11.0 requires a NUL, but an ASCII coefficient can fill
        # all 15 bytes as on the lab Maya2000 Pro's cubic coefficient.
        coefficients.append(float(reply[2:].split(b"\x00", 1)[0].decode("ascii")))
    pixels = np.arange(feature._spectrum_length, dtype=np.float64)
    return np.polynomial.polynomial.polyval(pixels, coefficients)


def _decode_usb650(raw: bytes) -> np.ndarray:
    if len(raw) != 4097 or raw[-1] != 0x69:
        raise RuntimeError(f"Invalid USB650 frame ({len(raw)} bytes, trailer {raw[-1:]!r}).")
    blocks = np.frombuffer(raw, dtype=np.uint8, count=4096).reshape(32, 128)
    low = blocks[:, :64].astype(np.uint16)
    high = blocks[:, 64:].astype(np.uint16) & 0x0F
    return (low | (high << 8)).ravel().astype(np.float64)


# LightKeeper MIT License
# Copyright (c) 2026 LightKeeper contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
