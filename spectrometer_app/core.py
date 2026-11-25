"""Spectrum acquisition, exact dark matching, and lossless CSV export."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
FlagArray = NDArray[np.int8]
DARK_GRID_US = tuple(range(10_000, 3_000_001, 10_000))
MAX_SCANS_TO_AVERAGE = 100
FORMAT_VERSION = 1
_OPUS_PATH_LIMIT = 220
_DARK_LOAD_ERRORS = (OSError, ValueError, TypeError, KeyError, OverflowError, EOFError, zipfile.BadZipFile)


@dataclass(frozen=True)
class InstrumentInfo:
    model: str
    serial: str
    backend: str
    min_integration_us: int
    max_integration_us: int
    max_counts: float
    integration_step_us: int = 1
    readout_mode: str = "device output (backend processing unspecified)"

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.model, self.serial, self.backend, self.readout_mode)):
            raise ValueError("Instrument model, serial, backend, and readout mode are required.")
        if self.min_integration_us <= 0 or self.max_integration_us < self.min_integration_us:
            raise ValueError("Invalid instrument integration range.")
        if self.integration_step_us <= 0 or not math.isfinite(self.max_counts) or self.max_counts <= 0:
            raise ValueError("Invalid instrument integration step or detector maximum.")

    def supports_integration(self, integration_us: int) -> bool:
        return (
            self.min_integration_us <= integration_us <= self.max_integration_us
            and (integration_us - self.min_integration_us) % self.integration_step_us == 0
        )


class Device(Protocol):
    info: InstrumentInfo
    wavelengths_nm: ArrayLike

    def set_integration_us(self, integration_us: int) -> int: ...

    def read_counts(self) -> ArrayLike: ...

    def close(self) -> None: ...


def _read_only_vector(values: ArrayLike, name: str) -> FloatArray:
    vector = np.array(values, dtype=np.float64, copy=True)
    if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
        raise ValueError(f"{name} must be a nonempty, finite one-dimensional array.")
    vector.setflags(write=False)
    return vector


def wavelength_grid_sha256(wavelengths_nm: ArrayLike) -> str:
    wavelengths = np.asarray(wavelengths_nm, dtype="<f8")
    return hashlib.sha256(wavelengths.tobytes(order="C")).hexdigest()


def detect_dark_pixels(dark_counts: ArrayLike) -> FlagArray:
    """Flag isolated dark anomalies as +1 hot or -1 cold, preserving every pixel."""
    counts = _read_only_vector(dark_counts, "Dark counts")
    flags = np.zeros(counts.size, dtype=np.int8)
    if counts.size >= 9:
        windows = np.lib.stride_tricks.sliding_window_view(counts, 9)
        neighbors = np.column_stack((windows[:, :4], windows[:, 5:]))
        residual = windows[:, 4] - np.median(neighbors, axis=1)
        center = np.median(residual)
        sigma = 1.4826 * np.median(np.abs(residual - center))
        threshold = max(8.0, 8.0 * sigma)
        interior = flags[4:-4]
        interior[residual > center + threshold] = 1
        interior[residual < center - threshold] = -1
    flags.setflags(write=False)
    return flags


def interpolate_flagged_pixels(counts: FloatArray, flags: FlagArray | None) -> FloatArray:
    """Bridge marked detector pixels by index and leave unbracketed ones undefined."""
    if flags is None or not np.any(flags):
        return counts
    interpolated = counts.copy()
    marked = np.flatnonzero(flags)
    good = np.flatnonzero(flags == 0)
    if good.size:
        interpolated[marked] = np.interp(marked, good, counts[good], left=np.nan, right=np.nan)
    else:
        interpolated[marked] = np.nan
    return interpolated


@dataclass(frozen=True)
class DarkRecord:
    integration_us: int
    counts: FloatArray
    pixel_flags: FlagArray
    profile_id: str
    averages: int
    acquired_at: datetime
    label: str | None = None
    temperature_c: float | None = None
    temperature_source: str | None = None


@dataclass(frozen=True)
class DarkProfileSummary:
    profile_id: str
    path: Path
    published_at: datetime
    acquired_at: datetime
    integration_us: tuple[int, ...]
    averages: int
    label: str | None = None
    temperature_c: float | None = None
    temperature_source: str | None = None


@dataclass(frozen=True)
class UsableBounds:
    lower_nm: float
    upper_nm: float
    source: str
    reason: str

    @property
    def bounds_nm(self) -> tuple[float, float]:
        return self.lower_nm, self.upper_nm


@dataclass(frozen=True)
class _LoadedProfile:
    summary: DarkProfileSummary
    records: dict[int, DarkRecord]


@dataclass(frozen=True)
class Frame:
    info: InstrumentInfo
    wavelengths_nm: FloatArray
    integration_us: int
    raw_counts: FloatArray
    started_at: datetime
    acquired_at: datetime
    started_monotonic: float
    acquired_monotonic: float
    dark_record: DarkRecord | None = None
    corrected_counts: FloatArray | None = None
    dark_subtraction_requested: bool = False
    scans_to_average: int = 1
    constituent_counts: FloatArray | None = None

    @property
    def display_counts(self) -> FloatArray:
        return self.raw_counts if self.corrected_counts is None else self.corrected_counts

    @property
    def pixel_flags(self) -> FlagArray | None:
        return None if self.dark_record is None else self.dark_record.pixel_flags

    @property
    def saturation_mask(self) -> NDArray[np.bool_]:
        if self.constituent_counts is not None:
            return np.any(self.constituent_counts >= self.info.max_counts, axis=0)
        return self.raw_counts >= self.info.max_counts

    @property
    def saturated_pixels(self) -> int:
        return int(np.count_nonzero(self.saturation_mask))

    @property
    def saturated_scan_counts(self) -> NDArray[np.intp]:
        """Count clipped constituent scans independently for each detector pixel."""
        if self.constituent_counts is None:
            return np.asarray(self.raw_counts >= self.info.max_counts, dtype=np.intp)
        return np.count_nonzero(self.constituent_counts >= self.info.max_counts, axis=0)

    @property
    def saturated_scans(self) -> int:
        if self.constituent_counts is None:
            return int(np.any(self.saturation_mask))
        return int(np.count_nonzero(np.any(self.constituent_counts >= self.info.max_counts, axis=1)))


def acquire_frame(
    device: Device,
    integration_us: int,
    dark_library: DarkLibrary | None = None,
    *,
    subtract_dark: bool = False,
    scans_to_average: int = 1,
    cancelled: Callable[[], bool] | None = None,
) -> Frame:
    if not isinstance(integration_us, (int, np.integer)) or not device.info.supports_integration(integration_us):
        raise ValueError(f"The instrument does not support an integration time of {integration_us} µs.")
    if (
        isinstance(scans_to_average, bool)
        or not isinstance(scans_to_average, (int, np.integer))
        or not 1 <= scans_to_average <= MAX_SCANS_TO_AVERAGE
    ):
        raise ValueError(f"Scans to average must be an integer from 1 to {MAX_SCANS_TO_AVERAGE}.")
    integration_us = int(integration_us)
    scans_to_average = int(scans_to_average)
    wavelengths_nm = _read_only_vector(device.wavelengths_nm, "Wavelengths in nm")
    actual_integration_us = device.set_integration_us(integration_us)
    if actual_integration_us != integration_us:
        raise RuntimeError(
            f"The instrument returned {actual_integration_us} µs for the requested {integration_us} µs."
        )
    if cancelled is not None and cancelled():
        raise CancelledError
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    constituents = None if scans_to_average == 1 else np.empty((scans_to_average, wavelengths_nm.size))
    for scan in range(scans_to_average):
        counts = device.read_counts()
        if cancelled is not None and cancelled():
            raise CancelledError
        raw_counts = _read_only_vector(counts, "Spectrum counts")
        if raw_counts.shape != wavelengths_nm.shape:
            raise ValueError(f"Detector scan {scan + 1} length differs from its wavelength calibration.")
        if constituents is not None:
            constituents[scan] = raw_counts
    acquired_monotonic = time.monotonic()
    acquired_at = datetime.now(timezone.utc)
    if constituents is not None:
        raw_counts = np.mean(constituents, axis=0)
        if not np.isfinite(raw_counts).all():
            raise ValueError("Averaged counts contain nonfinite values.")
        raw_counts.setflags(write=False)
        constituents.setflags(write=False)
    dark_record = None if dark_library is None else dark_library.find(device.info, wavelengths_nm, integration_us)
    corrected_counts = None
    if subtract_dark and dark_record is not None:
        corrected_counts = raw_counts - dark_record.counts
        corrected_counts.setflags(write=False)
    if cancelled is not None and cancelled():
        raise CancelledError
    return Frame(
        device.info,
        wavelengths_nm,
        integration_us,
        raw_counts,
        started_at,
        acquired_at,
        started_monotonic,
        acquired_monotonic,
        dark_record,
        corrected_counts,
        subtract_dark,
        scans_to_average,
        constituents,
    )


def _dark_identity(info: InstrumentInfo, wavelengths_nm: ArrayLike) -> dict:
    return {
        "model": info.model,
        "serial": info.serial,
        "backend": info.backend,
        "readout_mode": info.readout_mode,
        "max_counts": float(info.max_counts),
        "wavelength_grid_sha256": wavelength_grid_sha256(wavelengths_nm),
    }


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_for_filename(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _new_file(temp_path: Path, final_path: Path) -> None:
    """Publish a finished file without replacing an existing destination."""
    if os.name == "nt":
        temp_path.rename(final_path)
    else:
        os.link(temp_path, final_path)


def _utc_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Dark acquisition time is not in UTC.")
    return timestamp


def _session_tags(
    label: str | None, temperature_c: float | None, temperature_source: str | None
) -> tuple[str | None, float | None, str | None]:
    if label is not None:
        if not isinstance(label, str) or any(ord(char) < 32 for char in label):
            raise ValueError("Use a single-line label for the dark library.")
        label = label.strip() or None
    if temperature_c is None:
        if temperature_source is not None:
            raise ValueError("Enter a measured temperature or leave its source blank.")
    elif (
        type(temperature_c) not in (int, float)
        or not math.isfinite(temperature_c)
        or temperature_c < -273.15
        or temperature_source not in ("ambient", "instrument")
    ):
        raise ValueError("Enter a measured temperature in °C with an ambient or instrument source.")
    return label, temperature_c, temperature_source


def _flat_edge_length(spectra: FloatArray, allow_nonzero: bool) -> int:
    first_value = spectra[0, 0]
    if first_value != 0 and not allow_nonzero:
        return 0
    varies = np.flatnonzero(np.any(spectra != first_value, axis=0))
    if not varies.size:
        return 0
    length = int(varies[0])
    adjacent = spectra[:, length:length + 8]
    if length < 16 or adjacent.shape[1] < 8:
        return 0
    # Demand a contiguous change across all eight channels beside a flat edge.
    if first_value == 0:
        if np.min(np.median(np.abs(adjacent), axis=0)) > 32:
            return length
    elif np.min(np.ptp(adjacent, axis=0)) > 32:
        return length
    return 0


def infer_usable_bounds_nm(
    info: InstrumentInfo,
    wavelengths_nm: ArrayLike,
    counts: ArrayLike | None = None,
    integration_us: ArrayLike | None = None,
) -> UsableBounds | None:
    """Infer reversible display bounds from documented channels or decisive padding."""
    wavelengths = _read_only_vector(wavelengths_nm, "Wavelengths in nm")
    differences = np.diff(wavelengths)
    if not differences.size or not (np.all(differences > 0) or np.all(differences < 0)):
        return None
    start, stop = 0, wavelengths.size
    reasons = []
    sources = []
    model = re.sub(r"[^a-z0-9]", "", info.model.casefold())
    if model == "maya2000pro" and "seabreeze" in info.backend.casefold() and wavelengths.size == 2304:
        # Ocean Optics OEM data sheet, March 2016, p. 7. Channel indices
        # are independent of the device's wavelength calibration.
        start, stop = 10, 2058
        reasons.append("Maya2000 Pro spectrum channels 10–2057 (Ocean Optics OEM data sheet)")
        sources.append("documented detector layout")
    if counts is not None:
        spectra = np.atleast_2d(np.asarray(counts, dtype=np.float64))
        if spectra.ndim != 2 or spectra.shape[1] != wavelengths.size or not spectra.shape[0]:
            raise ValueError("Expected one or more detector-order count arrays.")
        if not np.isfinite(spectra).all():
            raise ValueError("Counts for display bounds must be finite.")
        exposures = None if integration_us is None else np.asarray(integration_us)
        if exposures is not None and exposures.shape != (spectra.shape[0],):
            raise ValueError("Dark counts need one integration time per row.")
        allow_nonzero = (
            exposures is not None
            and exposures.size >= 12
            and np.ptp(exposures) >= 100_000
        )
        active = spectra[:, start:stop]
        left = _flat_edge_length(active, allow_nonzero)
        right = _flat_edge_length(active[:, ::-1], allow_nonzero)
        if 0 < left + right <= active.shape[1] // 2:
            start += left
            stop -= right
            reasons.append(f"flat detector edges spanning {left + right} channels")
            sources.append("dark library" if exposures is not None else "live counts")
    if not reasons:
        return None
    lower, upper = sorted((float(wavelengths[start]), float(wavelengths[stop - 1])))
    return UsableBounds(lower, upper, " and ".join(sources), ". ".join(reasons))


class DarkLibrary:
    """Immutable dark sweeps, with an optional persistent selection per setup."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.load_error: str | None = None
        self._cached: dict[Path, tuple[int, _LoadedProfile | None, str | None]] = {}

    def _profile_directory(self, identity: dict) -> Path:
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        return self.directory / key

    @staticmethod
    def _read_selection(directory: Path) -> str | None:
        try:
            selection = json.loads((directory / "selection.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (
            not isinstance(selection, dict)
            or selection.get("format") != FORMAT_VERSION
            or "selected_profile_id" not in selection
        ):
            raise ValueError("Unsupported dark profile selection.")
        profile_id = selection["selected_profile_id"]
        if profile_id is not None and (
            not isinstance(profile_id, str) or re.fullmatch(r"[\w.-]+", profile_id) is None
        ):
            raise ValueError("Invalid saved dark profile ID.")
        return profile_id

    def selected_profile_id(self, info: InstrumentInfo, wavelengths_nm: ArrayLike) -> str | None:
        """Return the pinned ID. None means automatic use of the newest session."""
        directory = self._profile_directory(_dark_identity(info, wavelengths_nm))
        return self._read_selection(directory)

    def select_profile(
        self, info: InstrumentInfo, wavelengths_nm: ArrayLike, profile_id: str | None
    ) -> None:
        identity = _dark_identity(info, wavelengths_nm)
        directory = self._profile_directory(identity)
        if profile_id is not None:
            if not isinstance(profile_id, str) or re.fullmatch(r"[\w.-]+", profile_id) is None:
                raise ValueError("Invalid dark profile ID.")
            path = directory / f"{profile_id}.npz"
            if not path.is_file():
                raise ValueError("The selected dark profile is unavailable for this instrument.")
            self._read_profile(path, identity, np.asarray(wavelengths_nm).size)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix="selection_", suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
            try:
                json.dump({"format": FORMAT_VERSION, "selected_profile_id": profile_id}, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp_path, directory / "selection.json")
        finally:
            tmp_path.unlink(missing_ok=True)
        self._cached.pop(directory, None)

    def list_profiles(self, info: InstrumentInfo, wavelengths_nm: ArrayLike) -> list[DarkProfileSummary]:
        identity = _dark_identity(info, wavelengths_nm)
        directory = self._profile_directory(identity)
        self.load_error = None
        summaries = []
        for path in directory.glob("*.npz"):
            try:
                with np.load(path, allow_pickle=False) as profile:
                    summary, _ = self._read_summary(profile, path, identity, np.asarray(wavelengths_nm).size)
                summaries.append(summary)
            except _DARK_LOAD_ERRORS as exc:
                self.load_error = f"Could not list dark profile {path.name}: {exc}"
        return sorted(summaries, key=lambda summary: (summary.published_at, summary.profile_id), reverse=True)

    def active_profile(
        self, info: InstrumentInfo, wavelengths_nm: ArrayLike
    ) -> DarkProfileSummary | None:
        loaded = self._active_profile(info, wavelengths_nm)
        return None if loaded is None else loaded.summary

    def infer_usable_bounds_nm(
        self, info: InstrumentInfo, wavelengths_nm: ArrayLike, profile_id: str | None = None
    ) -> UsableBounds | None:
        if profile_id is None:
            loaded = self._active_profile(info, wavelengths_nm)
        else:
            identity = _dark_identity(info, wavelengths_nm)
            if not isinstance(profile_id, str) or re.fullmatch(r"[\w.-]+", profile_id) is None:
                raise ValueError("Invalid dark profile ID.")
            path = self._profile_directory(identity) / f"{profile_id}.npz"
            loaded = self._read_profile(path, identity, np.asarray(wavelengths_nm).size)
        if loaded is None:
            return infer_usable_bounds_nm(info, wavelengths_nm)
        exposures = loaded.summary.integration_us
        counts = np.stack([loaded.records[exposure].counts for exposure in exposures])
        return infer_usable_bounds_nm(info, wavelengths_nm, counts, exposures)

    def find(self, info: InstrumentInfo, wavelengths_nm: ArrayLike, integration_us: int) -> DarkRecord | None:
        if integration_us not in DARK_GRID_US:
            self.load_error = None
            return None
        loaded = self._active_profile(info, wavelengths_nm)
        return None if loaded is None else loaded.records.get(integration_us)

    def _active_profile(
        self, info: InstrumentInfo, wavelengths_nm: ArrayLike
    ) -> _LoadedProfile | None:
        self.load_error = None
        identity = _dark_identity(info, wavelengths_nm)
        directory = self._profile_directory(identity)
        try:
            stamp = directory.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError as exc:
            self.load_error = f"Could not access the dark library: {exc}"
            return None
        cached = self._cached.get(directory)
        if cached is None or cached[0] != stamp:
            loaded = None
            error = None
            path = None
            try:
                pinned = self._read_selection(directory)
                path = directory / f"{pinned}.npz" if pinned is not None else max(directory.glob("*.npz"), default=None)
                if path is not None:
                    loaded = self._read_profile(path, identity, np.asarray(wavelengths_nm).size)
            except _DARK_LOAD_ERRORS as exc:
                operation = f"load dark profile {path.name}" if path is not None else "read dark profile selection"
                error = f"Could not {operation}: {exc}"
            cached = (stamp, loaded, error)
            self._cached[directory] = cached
        self.load_error = cached[2]
        return cached[1]

    @staticmethod
    def _read_summary(
        profile, path: Path, identity: dict, pixels: int
    ) -> tuple[DarkProfileSummary, tuple[datetime, ...]]:
        metadata = json.loads(profile["metadata"].item())
        exposures = profile["integration_us"]
        wavelengths = _read_only_vector(profile["wavelength_nm"], "Dark wavelength calibration")
        if (
            metadata["format"] != FORMAT_VERSION
            or metadata["identity"] != identity
            or wavelength_grid_sha256(wavelengths) != identity["wavelength_grid_sha256"]
            or wavelengths.size != pixels
        ):
            raise ValueError("Dark profile identity or wavelength calibration differs.")
        if (
            exposures.ndim != 1
            or not np.issubdtype(exposures.dtype, np.integer)
            or not exposures.size
            or len(set(exposures)) != exposures.size
            or any(exposure not in DARK_GRID_US for exposure in exposures)
            or len(metadata["acquired_at"]) != exposures.size
            or not isinstance(metadata["averages"], int)
            or metadata["averages"] < 1
            or metadata["profile_id"] != path.stem
        ):
            raise ValueError("Dark profile exposure metadata is invalid.")
        stamps = tuple(_utc_timestamp(stamp) for stamp in metadata["acquired_at"])
        label, temperature_c, temperature_source = _session_tags(
            metadata.get("label"), metadata.get("temperature_c"), metadata.get("temperature_source")
        )
        return DarkProfileSummary(
            path.stem,
            path,
            _utc_timestamp(metadata["published_at"]),
            max(stamps),
            tuple(int(exposure) for exposure in exposures),
            metadata["averages"],
            label,
            temperature_c,
            temperature_source,
        ), stamps

    @classmethod
    def _read_profile(cls, path: Path, identity: dict, pixels: int) -> _LoadedProfile:
        with np.load(path, allow_pickle=False) as profile:
            summary, stamps = cls._read_summary(profile, path, identity, pixels)
            counts = profile["counts"]
        if counts.shape != (len(summary.integration_us), pixels):
            raise ValueError("Dark profile count array has the wrong dimensions.")
        records = {}
        for exposure, values, stamp in zip(summary.integration_us, counts, stamps, strict=True):
            values = _read_only_vector(values, "Stored dark counts")
            records[exposure] = DarkRecord(
                exposure,
                values,
                detect_dark_pixels(values),
                summary.profile_id,
                summary.averages,
                stamp,
                summary.label,
                summary.temperature_c,
                summary.temperature_source,
            )
        return _LoadedProfile(summary, records)

    def start_session(
        self,
        device: Device,
        averages: int = 1,
        *,
        label: str | None = None,
        temperature_c: float | None = None,
        temperature_source: str | None = None,
    ) -> DarkSession:
        tags = _session_tags(label, temperature_c, temperature_source)
        return DarkSession(self, device, averages, tags)

    def _publish(
        self,
        info: InstrumentInfo,
        wavelengths_nm: FloatArray,
        integration_us: tuple[int, ...],
        averages: int,
        rows: list[FloatArray],
        acquired_at: list[datetime],
        tags: tuple[str | None, float | None, str | None],
    ) -> Path:
        identity = _dark_identity(info, wavelengths_nm)
        directory = self._profile_directory(identity)
        directory.mkdir(parents=True, exist_ok=True)
        published_at = datetime.now(timezone.utc)
        profile_id = f"{_timestamp_for_filename(published_at)}_{uuid.uuid4().hex}"
        metadata = {
            "format": FORMAT_VERSION,
            "identity": identity,
            "profile_id": profile_id,
            "instrument": asdict(info),
            "averages": averages,
            "published_at": _iso_utc(published_at),
            "acquired_at": [_iso_utc(stamp) for stamp in acquired_at],
        }
        label, temperature_c, temperature_source = tags
        if label is not None:
            metadata["label"] = label
        if temperature_c is not None:
            metadata.update(temperature_c=temperature_c, temperature_source=temperature_source)
        final_path = directory / f"{profile_id}.npz"
        with tempfile.NamedTemporaryFile(dir=directory, prefix="dark_", suffix=".tmp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
                np.savez_compressed(
                    tmp,
                    metadata=json.dumps(metadata, ensure_ascii=False),
                    wavelength_nm=wavelengths_nm,
                    integration_us=integration_us,
                    counts=np.stack(rows),
                )
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise
        try:
            _new_file(tmp_path, final_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        self._cached.pop(directory, None)
        return final_path


class DarkSession:
    """Acquire each supported grid point without publishing an incomplete sweep."""

    def __init__(
        self,
        library: DarkLibrary,
        device: Device,
        averages: int,
        tags: tuple[str | None, float | None, str | None] = (None, None, None),
    ) -> None:
        if not isinstance(averages, int) or averages <= 0:
            raise ValueError("Dark averages must be a positive integer.")
        self._library = library
        self._device = device
        self._info = device.info
        self._wavelengths_nm = _read_only_vector(device.wavelengths_nm, "Wavelengths in nm")
        self._wavelength_hash = wavelength_grid_sha256(self._wavelengths_nm)
        self._integrations = tuple(value for value in DARK_GRID_US if self._info.supports_integration(value))
        if not self._integrations:
            raise ValueError("The instrument supports none of the 10–3000 ms dark grid points.")
        self.skipped_integration_us = tuple(value for value in DARK_GRID_US if value not in self._integrations)
        self._averages = averages
        self._tags = tags
        self.captures_total = len(self._integrations) * averages
        self.captures_done = 0
        self.published_path: Path | None = None
        self.failed = False
        self._cancelled = threading.Event()
        self._step_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._rows: list[FloatArray] = []
        self._acquired_at: list[datetime] = []
        self._sum = np.zeros_like(self._wavelengths_nm)

    @property
    def completed(self) -> bool:
        return self.published_path is not None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def next_integration_us(self) -> int | None:
        if self.cancelled or self.failed or self.captures_done == self.captures_total:
            return None
        return self._integrations[self.captures_done // self._averages]

    @property
    def remaining_integration_s(self) -> float:
        if self.next_integration_us is None:
            return 0.0
        index, captures_at_point = divmod(self.captures_done, self._averages)
        remaining_us = self._averages * sum(self._integrations[index:])
        remaining_us -= captures_at_point * self._integrations[index]
        return remaining_us / 1_000_000

    def cancel(self) -> None:
        with self._publish_lock:
            if not self.completed:
                self._cancelled.set()

    def step(self) -> Frame | None:
        with self._step_lock:
            integration_us = self.next_integration_us
            if integration_us is None:
                return None
            try:
                frame = acquire_frame(self._device, integration_us)
                if frame.info != self._info or wavelength_grid_sha256(frame.wavelengths_nm) != self._wavelength_hash:
                    raise RuntimeError("The instrument or wavelength calibration changed during the dark session.")
                if self.cancelled:
                    return None
                self._sum += frame.raw_counts
                self.captures_done += 1
                if self.captures_done % self._averages == 0:
                    self._rows.append(self._sum / self._averages)
                    self._acquired_at.append(frame.acquired_at)
                    self._sum.fill(0)
                if self.captures_done == self.captures_total:
                    with self._publish_lock:
                        if not self.cancelled:
                            self.published_path = self._library._publish(
                                self._info,
                                self._wavelengths_nm,
                                self._integrations,
                                self._averages,
                                self._rows,
                                self._acquired_at,
                                self._tags,
                            )
                return frame
            except Exception:
                self.failed = True
                raise


def _capture_metadata(
    frame: Frame,
    title: str,
    run_id: str | None,
    sequence: int | None,
    scheduled_offset_s: float | None,
    scheduled_monotonic: float | None,
    run_started_monotonic: float | None,
) -> dict[str, str | int | float]:
    dark_subtraction = "off"
    if frame.corrected_counts is not None:
        dark_subtraction = "raw_counts - dark_counts"
    elif frame.dark_subtraction_requested:
        dark_subtraction = "requested, exact dark unavailable"
    metadata = {
        "format": FORMAT_VERSION,
        "title": title,
        "instrument_model": frame.info.model,
        "instrument_serial": frame.info.serial,
        "backend": frame.info.backend,
        "readout_mode": frame.info.readout_mode,
        "integration_time_us": frame.integration_us,
        "scans_to_average": frame.scans_to_average,
        "scan_averaging": "arithmetic mean of consecutive detector scans" if frame.scans_to_average > 1 else "none",
        "read_started_utc": _iso_utc(frame.started_at),
        "read_finished_utc": _iso_utc(frame.acquired_at),
        "read_elapsed_s": frame.acquired_monotonic - frame.started_monotonic,
        "wavelength_grid_sha256": wavelength_grid_sha256(frame.wavelengths_nm),
        "wavelength_order": "instrument detector order",
        "raw_counts": (
            "unsmoothed arithmetic mean of the device-returned scans before dark or pixel corrections"
            if frame.scans_to_average > 1 else "device-returned counts, unchanged by this application"
        ),
        "raw_saturated_pixels": frame.saturated_pixels,
        "raw_saturated_scans": frame.saturated_scans,
        "detector_max_counts": frame.info.max_counts,
        "dark_subtraction": dark_subtraction,
        "smoothing_resampling_normalization_pixel_replacement": "none",
    }
    if frame.dark_record is not None:
        metadata.update(
            dark_profile_id=frame.dark_record.profile_id,
            dark_acquired_utc=_iso_utc(frame.dark_record.acquired_at),
            dark_averages=frame.dark_record.averages,
            pixel_flag_method="dark residual from median of 4 neighbors on each side, "
            "8 robust sigma with 8-count minimum, first and last 4 pixels unclassified",
        )
        if frame.dark_record.label is not None:
            metadata["dark_profile_label"] = frame.dark_record.label
        if frame.dark_record.temperature_c is not None:
            metadata["dark_temperature_c"] = frame.dark_record.temperature_c
            metadata["dark_temperature_source"] = frame.dark_record.temperature_source
            metadata["dark_temperature_entry"] = "manual"
    if run_id is not None:
        metadata["run_id"] = run_id
    if sequence is not None:
        metadata["sequence"] = sequence
    if scheduled_offset_s is not None:
        metadata["nominal_run_offset_s"] = scheduled_offset_s
    if scheduled_monotonic is not None:
        metadata["read_start_delay_from_due_s"] = frame.started_monotonic - scheduled_monotonic
    if run_started_monotonic is not None:
        metadata["read_start_offset_from_run_s"] = frame.started_monotonic - run_started_monotonic
    return metadata


def _validate_export_title(title: str) -> None:
    if not title.strip() or any(ord(char) < 32 for char in title):
        raise ValueError("Enter a spectrum title on a single line.")


def save_spectrum(
    frame: Frame,
    directory: Path | str,
    title: str,
    *,
    run_id: str | None = None,
    sequence: int | None = None,
    scheduled_offset_s: float | None = None,
    scheduled_monotonic: float | None = None,
    run_started_monotonic: float | None = None,
) -> Path:
    """Save the detector-order vectors in one UTF-8 CSV with comment metadata."""
    _validate_export_title(title)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    filename_title = re.sub(r'[^\w.\-]+', "_", title.strip()).strip("._-")[:72] or "spectrum"
    suffix = f"_{sequence:04d}" if sequence is not None else ""
    final_path = directory / (
        f"{_timestamp_for_filename(frame.started_at)}_{filename_title}{suffix}_{uuid.uuid4().hex[:10]}.csv"
    )
    metadata = _capture_metadata(
        frame, title, run_id, sequence, scheduled_offset_s, scheduled_monotonic, run_started_monotonic
    )

    columns = ["pixel_index", "wavelength_nm", "raw_counts"]
    if frame.corrected_counts is not None:
        columns.extend(("dark_counts", "dark_corrected_counts"))
    if frame.pixel_flags is not None:
        columns.append("pixel_flag")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=directory, prefix="spectrum_", suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            writer = csv.writer(tmp)
            for key, value in metadata.items():
                writer.writerow((f"# {key}", value))
            writer.writerow(columns)
            labels = {-1: "cold", 0: "", 1: "hot"}
            for index, (wavelength, raw) in enumerate(zip(frame.wavelengths_nm, frame.raw_counts, strict=True)):
                row = [index, wavelength, raw]
                if frame.corrected_counts is not None and frame.dark_record is not None:
                    row.extend((frame.dark_record.counts[index], frame.corrected_counts[index]))
                if frame.pixel_flags is not None:
                    row.append(labels[int(frame.pixel_flags[index])])
                writer.writerow(row)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        _new_file(tmp_path, final_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return final_path


@dataclass(frozen=True)
class SpectrumFiles:
    raw: Path
    corrected: Path
    details: Path


def _pair_export_paths(
    frame: Frame, directory: Path, title: str, sequence: int | None, identity: bool
) -> SpectrumFiles:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_title).strip("_") or "spectrum"
    stamp = _timestamp_for_filename(frame.started_at)
    sequence_token = f"_{sequence:04d}" if sequence is not None else ""
    unique = uuid.uuid4().hex[:10]
    corrected_role = "corrected_identity" if identity else "corrected"
    suffix = f"{sequence_token}_{unique}_{corrected_role}.csv"
    title_budget = _OPUS_PATH_LIMIT - len(str((directory / f"{stamp}_{suffix}").absolute()))
    if title_budget < 1:
        raise OSError("Choose a shorter output folder to stay within the legacy OPUS path limit of 220 characters.")
    slug = slug[:min(48, title_budget)].rstrip("_")
    stem = f"{stamp}_{slug}{sequence_token}_{unique}"
    return SpectrumFiles(
        directory / f"{stem}_raw.csv",
        directory / f"{stem}_{corrected_role}.csv",
        directory / "Details" / f"{stem}_details.csv",
    )


def _write_xy(path: Path, wavelengths_nm: FloatArray, counts: FloatArray) -> None:
    with path.open("x", encoding="ascii", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(("Wavelength (nm)", "Amplitude"))
        writer.writerows(zip(wavelengths_nm.tolist(), counts.tolist(), strict=True))
        stream.flush()
        os.fsync(stream.fileno())


def _write_pair_details(
    path: Path,
    frame: Frame,
    metadata: dict[str, str | int | float],
    exported_counts: FloatArray,
    interpolated: NDArray[np.bool_],
    unbracketed: NDArray[np.bool_],
) -> None:
    flags = frame.pixel_flags
    dark = frame.dark_record.counts if frame.dark_record is not None else None
    dark_corrected = frame.corrected_counts
    saturated = frame.saturation_mask
    saturated_scan_counts = frame.saturated_scan_counts
    constituents = frame.constituent_counts
    flag_labels = {-1: "cold", 0: "", 1: "hot"}
    columns = (
        "pixel_index", "wavelength_nm", "raw_counts", "dark_counts", "dark_corrected_counts",
        "corrected_export_counts", "pixel_flag", "pixel_interpolation", "raw_saturated",
    )
    if constituents is not None:
        columns += ("raw_saturated_scan_count",)
        columns += tuple(f"raw_scan_{number:03d}_counts" for number in range(1, frame.scans_to_average + 1))
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows((f"# {key}", value) for key, value in metadata.items())
        writer.writerow(columns)
        for channel, (wavelength, raw, exported) in enumerate(
            zip(frame.wavelengths_nm, frame.raw_counts, exported_counts, strict=True)
        ):
            treatment = ""
            if interpolated[channel]:
                treatment = "interpolated"
            elif unbracketed[channel]:
                treatment = "retained_unbracketed"
            row = (
                channel, wavelength, raw,
                "" if dark is None else dark[channel],
                "" if dark_corrected is None else dark_corrected[channel],
                exported,
                "" if flags is None else flag_labels[int(flags[channel])],
                treatment,
                int(saturated[channel]),
            )
            if constituents is not None:
                row += (saturated_scan_counts[channel], *constituents[:, channel].tolist())
            writer.writerow(row)
        stream.flush()
        os.fsync(stream.fileno())


def save_spectrum_pair(
    frame: Frame,
    directory: Path | str,
    title: str,
    *,
    interpolate_pixels: bool = False,
    run_id: str | None = None,
    sequence: int | None = None,
    scheduled_offset_s: float | None = None,
    scheduled_monotonic: float | None = None,
    run_started_monotonic: float | None = None,
) -> SpectrumFiles:
    """Write both detector-order XY files and their matching acquisition record."""
    _validate_export_title(title)
    if frame.dark_subtraction_requested and frame.corrected_counts is None:
        raise ValueError("A matching dark is required to save this spectrum with subtraction enabled.")
    wavelengths = _read_only_vector(frame.wavelengths_nm, "Wavelengths in nm")
    raw_counts = _read_only_vector(frame.raw_counts, "Raw counts")
    base_counts = _read_only_vector(frame.display_counts, "Counts before pixel interpolation")
    if raw_counts.shape != wavelengths.shape or base_counts.shape != wavelengths.shape:
        raise ValueError("Wavelengths and counts must have equal lengths.")
    constituents = frame.constituent_counts
    if constituents is None:
        if frame.scans_to_average != 1:
            raise ValueError("The averaged spectrum is missing its constituent scans.")
    elif (
        not 2 <= frame.scans_to_average <= MAX_SCANS_TO_AVERAGE
        or constituents.shape != (frame.scans_to_average, wavelengths.size)
        or not np.isfinite(constituents).all()
        or not np.array_equal(raw_counts, np.mean(constituents, axis=0))
    ):
        raise ValueError("The constituent scans do not match the averaged spectrum.")
    if np.unique(wavelengths).size != wavelengths.size:
        raise ValueError("OPUS spectrum CSVs require distinct wavelength coordinates.")
    if frame.dark_record is not None:
        dark_counts = _read_only_vector(frame.dark_record.counts, "Dark counts")
        if frame.dark_record.integration_us != frame.integration_us or dark_counts.shape != wavelengths.shape:
            raise ValueError("The dark reference does not match the exposure and wavelength grid.")
        if frame.corrected_counts is not None and not np.array_equal(base_counts, raw_counts - dark_counts):
            raise ValueError("The dark-corrected spectrum differs from the raw spectrum minus its dark.")
    elif frame.corrected_counts is not None:
        raise ValueError("The corrected spectrum is missing its source dark reference.")
    flags = frame.pixel_flags
    if flags is not None and (flags.shape != wavelengths.shape or not np.isin(flags, (-1, 0, 1)).all()):
        raise ValueError("The pixel flags do not match the detector readout.")

    exported_counts = interpolate_flagged_pixels(base_counts, flags) if interpolate_pixels else base_counts
    unbracketed = np.isnan(exported_counts)
    interpolated = ((flags != 0) & ~unbracketed) if interpolate_pixels and flags is not None else np.zeros_like(unbracketed)
    exported_counts = np.where(unbracketed, base_counts, exported_counts)
    if not np.isfinite(exported_counts).all():
        raise ValueError("The corrected spectrum contains nonfinite values.")
    interpolated_count = int(np.count_nonzero(interpolated))
    has_dark_subtraction = frame.corrected_counts is not None
    identity = not (has_dark_subtraction or interpolated_count)

    files = _pair_export_paths(frame, Path(directory), title, sequence, identity)
    metadata = _capture_metadata(
        frame, title, run_id, sequence, scheduled_offset_s, scheduled_monotonic, run_started_monotonic
    )
    del metadata["smoothing_resampling_normalization_pixel_replacement"]
    metadata.update(
        xy_schema="ASCII, comma delimiter, CRLF, wavelength in nm, amplitude in counts",
        raw_file=files.raw.name,
        corrected_file=files.corrected.name,
        corrected_companion="identity, same measured counts" if identity else "see correction fields",
        smoothing_resampling_normalization="none",
        pixel_interpolation_requested=interpolate_pixels,
        pixel_interpolation=(
            "linear by detector index between unflagged pixels" if interpolated_count else "none"
        ),
        interpolated_channels=interpolated_count,
        unbracketed_channels_retained=int(np.count_nonzero(unbracketed)),
        unbracketed_pixel_policy="preserve the corresponding pre-interpolation count",
        saturation_rule="any constituent count >= detector_max_counts",
    )
    details_directory = files.details.parent
    details_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=details_directory, prefix=".capture-") as stage:
        staging = Path(stage)
        raw_stage, corrected_stage, details_stage = (
            staging / "raw.csv", staging / "corrected.csv", staging / "details.csv"
        )
        _write_xy(raw_stage, wavelengths, raw_counts)
        _write_xy(corrected_stage, wavelengths, exported_counts)
        _write_pair_details(details_stage, frame, metadata, exported_counts, interpolated, unbracketed)
        published = []
        try:
            for source, destination in ((details_stage, files.details), (raw_stage, files.raw),
                                        (corrected_stage, files.corrected)):
                _new_file(source, destination)
                published.append(destination)
        except BaseException as error:
            remaining = []
            for destination in reversed(published):
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    remaining.append(str(destination))
            if remaining:
                raise OSError(f"Save failed and partial files could not be removed: {', '.join(remaining)}") from error
            raise
    return files


class RepeatSchedule:
    """Capture immediately, then space read starts by at least the requested interval."""

    def __init__(self, count: int, interval_s: float, start_monotonic: float | None = None) -> None:
        if not isinstance(count, int) or count <= 0 or not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("Repeat count and interval must be positive.")
        self.count = count
        self.interval_s = interval_s
        self.run_id = uuid.uuid4().hex
        self.saved = 0
        self.start_monotonic = time.monotonic() if start_monotonic is None else start_monotonic
        if not math.isfinite(self.start_monotonic):
            raise ValueError("The repeat schedule needs a finite start time.")
        self._next_due = self.start_monotonic

    @property
    def next_due_monotonic(self) -> float | None:
        return None if self.done else self._next_due

    @property
    def scheduled_offset_s(self) -> float:
        return self.saved * self.interval_s

    @property
    def done(self) -> bool:
        return self.saved == self.count

    def due(self, now: float) -> bool:
        return not self.done and math.isfinite(now) and now >= self._next_due

    def record_saved(self, started_monotonic: float) -> None:
        if not self.due(started_monotonic):
            raise ValueError("The saved exposure started before its scheduled time or the run is complete.")
        self.saved += 1
        self._next_due = started_monotonic + self.interval_s


def suggest_integration_us(
    frame: Frame,
    *,
    use_dark_grid: bool = False,
    target_fraction: float = 0.75,
    candidate_integration_us: Iterable[int] | None = None,
    usable_bounds_nm: tuple[float, float] | None = None,
) -> int:
    """Propose an exposure from unsmoothed detector counts for a subsequent read."""
    if not 0 < target_fraction < 1:
        raise ValueError("The target detector fraction must be between zero and one.")
    valid = np.ones(frame.raw_counts.shape, dtype=bool)
    flags = frame.pixel_flags
    if flags is not None:
        valid &= flags == 0
    if usable_bounds_nm is not None:
        lower, upper = usable_bounds_nm
        valid &= (frame.wavelengths_nm >= lower) & (frame.wavelengths_nm <= upper)
    counts = frame.raw_counts[valid]
    if not counts.size:
        raise ValueError("No unflagged pixels are available in the auto-exposure range.")
    peak = float(np.max(counts))
    target = target_fraction * frame.info.max_counts
    saturated = np.any(frame.saturation_mask[valid])
    ratio = 0.5 if saturated else max(0.25, min(4.0, target / max(peak, 1.0)))
    desired = frame.integration_us if 0.9 <= ratio <= 1.1 else frame.integration_us * ratio
    if use_dark_grid or candidate_integration_us is not None:
        candidates = DARK_GRID_US if candidate_integration_us is None else candidate_integration_us
        grid = tuple(
            int(value)
            for value in candidates
            if value in DARK_GRID_US and frame.info.supports_integration(value)
        )
        if not grid:
            raise ValueError("No eligible dark integration times are available for auto exposure.")
        return min(grid, key=lambda value: (abs(value - desired), value))
    low = frame.info.min_integration_us
    step = frame.info.integration_step_us
    maximum_step = (frame.info.max_integration_us - low) // step
    return low + step * max(0, min(maximum_step, round((desired - low) / step)))


class DemoDevice:
    """Accelerated synthetic instrument for exercising the workflow without USB."""

    def __init__(self, model: str = "Maya2000Pro") -> None:
        if model not in {"Maya2000Pro", "USB650"}:
            raise ValueError(f"No demo is configured for {model}.")
        self.info = InstrumentInfo(
            model,
            f"DEMO-{model.upper()}",
            "demo",
            1_000,
            10_000_000,
            65535 if model == "Maya2000Pro" else 4095,
            readout_mode="accelerated simulation with synthetic dark signal and noise",
        )
        pixels = 2048
        self.wavelengths_nm = _read_only_vector(np.linspace(200, 1120, pixels), "Demo wavelengths")
        self._rng = np.random.default_rng(670 if model == "USB650" else 2070)
        self._covered = False
        self._closed = False
        self._integration_us = 100_000
        self._phase = 0.0
        self._profile = (
            np.exp(-0.5 * ((self.wavelengths_nm - 456) / 22) ** 2)
            + 0.65 * np.exp(-0.5 * ((self.wavelengths_nm - 562) / 51) ** 2)
            + 0.20 * np.exp(-0.5 * ((self.wavelengths_nm - 752) / 31) ** 2)
        )

    def set_covered(self, covered: bool) -> None:
        self._covered = covered

    def set_integration_us(self, integration_us: int) -> int:
        if self._closed:
            raise RuntimeError("The demo device is closed.")
        if not self.info.supports_integration(integration_us):
            raise ValueError("Demo integration time is outside its configured range.")
        self._integration_us = integration_us
        return integration_us

    def read_counts(self) -> FloatArray:
        if self._closed:
            raise RuntimeError("The demo device is closed.")
        exposure_s = self._integration_us / 1_000_000
        time.sleep(min(exposure_s, 0.04))
        self._phase += 0.12
        fraction = 1.0 + 0.018 * math.sin(self._phase)
        dark = 122 + 18 * exposure_s + 5 * np.sin(self.wavelengths_nm / 140)
        counts = dark + self._rng.normal(0, 2.7, dark.size)
        if not self._covered:
            counts += 5.8 * self.info.max_counts * exposure_s * fraction * self._profile
        counts[177] += 360 + 120 * exposure_s
        counts[1680] -= dark[1680] * 0.92
        return np.clip(np.rint(counts), 0, self.info.max_counts)

    def close(self) -> None:
        self._closed = True
