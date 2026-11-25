"""Parse human-entered quantities and format presentation-only readouts."""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from operator import index


_DIGITS = r"[0-9](?:_?[0-9])*"
_INTEGER = rf"(?:[0-9]{{1,3}}(?:,[0-9]{{3}})+|{_DIGITS})"
_QUANTITY = re.compile(
    rf"(?P<number>[+-]?(?:{_INTEGER}(?:\.(?:{_DIGITS})?)?|\.{_DIGITS})(?:[eE][+-]?{_DIGITS})?)"
    r"\s*(?P<suffix>.*)"
)

_TIME_SECONDS = {
    "ps": Decimal("1e-12"),
    "ns": Decimal("1e-9"),
    "µs": Decimal("1e-6"),
    "ms": Decimal("1e-3"),
    "s": Decimal(1),
    "min": Decimal(60),
    "h": Decimal(3600),
}
_TIME_ALIASES = {
    "ps": "ps", "picosecond": "ps", "picoseconds": "ps",
    "ns": "ns", "nanosecond": "ns", "nanoseconds": "ns",
    "µs": "µs", "us": "µs", "microsecond": "µs", "microseconds": "µs",
    "ms": "ms", "msec": "ms", "millisecond": "ms", "milliseconds": "ms",
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hour": "h", "hours": "h",
}
_WAVELENGTH_NM = {
    "nm": Decimal(1), "nanometer": Decimal(1), "nanometers": Decimal(1),
    "nanometre": Decimal(1), "nanometres": Decimal(1),
    "µm": Decimal(1000), "um": Decimal(1000), "micron": Decimal(1000), "microns": Decimal(1000),
    "micrometer": Decimal(1000), "micrometers": Decimal(1000),
    "micrometre": Decimal(1000), "micrometres": Decimal(1000),
    "å": Decimal("0.1"), "angstrom": Decimal("0.1"), "angstroms": Decimal("0.1"),
    "ångström": Decimal("0.1"), "ångströms": Decimal("0.1"),
}
_CELSIUS = {"°c", "c", "celsius", "degrees c", "degrees celsius"}
_FAHRENHEIT = {"°f", "f", "fahrenheit", "degrees f", "degrees fahrenheit"}
_KELVIN = {"k", "kelvin", "kelvins"}
_COUNT_SUFFIXES = {"spectra": {"spectrum", "spectra"}, "scans": {"scan", "scans"}}


def parse_quantity(text: str, unit: str) -> float:
    """Return a finite value in the specified unit, which is also the input default."""
    if unit not in ("ms", "s", "nm", "°C", "spectra", "scans"):
        raise ValueError(f"Unsupported quantity unit {unit!r}.")
    cleaned = text.strip().replace("−", "-")
    match = _QUANTITY.fullmatch(cleaned)
    if match is None:
        raise ValueError("Enter a finite number with an optional unit.")
    suffix = " ".join(match["suffix"].casefold().replace("μ", "µ").split())
    suffix = suffix.replace("° ", "°") or unit.casefold()
    try:
        original = Decimal(match["number"].replace(",", "").replace("_", ""))
        value = original
        if unit in ("ms", "s"):
            value = value * _TIME_SECONDS[_TIME_ALIASES[suffix]] / _TIME_SECONDS[unit]
        elif unit == "nm":
            value *= _WAVELENGTH_NM[suffix]
        elif unit == "°C":
            if suffix in _FAHRENHEIT:
                value = (value - 32) * 5 / 9
            elif suffix in _KELVIN:
                value -= Decimal("273.15")
            elif suffix not in _CELSIUS:
                raise KeyError(suffix)
        elif suffix not in _COUNT_SUFFIXES[unit]:
            raise KeyError(suffix)
        result = float(value)
    except KeyError:
        raise ValueError(f"Enter a value compatible with {unit}.") from None
    except (DecimalException, OverflowError):
        raise ValueError("Enter a finite number within the supported numeric range.") from None
    if not math.isfinite(result) or (
        result == 0 and (not value.is_zero() or (unit != "°C" and not original.is_zero()))
    ):
        raise ValueError("Enter a finite number within the supported numeric range.")
    return result


def format_number(value, *, decimals: int = 2) -> str:
    """Round for display, group thousands, and omit insignificant zeros."""
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Only finite values can be formatted.")
    with localcontext() as context:
        context.rounding = ROUND_HALF_UP
        text = f"{number:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").removesuffix(".")
    return "0" if text == "-0" else text


def format_quantity(value, unit: str) -> str:
    text = format_number(value)
    return f"{text} {unit}" if unit else text


def format_exposure(integration_us: int, *, exact: bool = False) -> str:
    """Use ms for routine readouts and exact µs when decimal ms obscure a setting."""
    microseconds = index(integration_us)
    if microseconds < 1000 or (exact and microseconds % 10):
        return format_quantity(microseconds, "µs")
    return format_quantity(Decimal(microseconds).scaleb(-3), "ms")
