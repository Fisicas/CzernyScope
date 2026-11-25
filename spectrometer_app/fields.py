"""Quantity entry with independent display formatting and committed values."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QAbstractSpinBox

from .quantities import format_number, format_quantity, parse_quantity


class QuantityField(QAbstractSpinBox):
    valueChanged = Signal(object)
    validityChanged = Signal(bool)

    def __init__(
        self, unit, minimum, maximum, value, *, examples, step=1,
        whole=False, optional=False, zero_text="",
    ):
        super().__init__()
        self.unit = unit
        self.examples = examples
        self.whole = whole
        self.optional = optional
        self.zero_text = zero_text
        self.step = step
        self._minimum, self._maximum = minimum, maximum
        self._value, self._shown = None, ""
        self._edited = False
        self._valid = True
        self.setKeyboardTracking(False)
        if step is None:
            self.setButtonSymbols(self.ButtonSymbols.NoButtons)
        editor = self.lineEdit()
        editor.setMaxLength(80)
        editor.textEdited.connect(self._mark_edited)
        editor.textChanged.connect(self._validate_input)
        self.editingFinished.connect(self.commit)
        self.setAccessibleDescription(f"Enter {examples}.")
        self.setValue(value)

    def validate(self, text, position):
        # Let users finish typing or paste units before applying our validation.
        return QValidator.State.Acceptable, text, position

    def _mark_edited(self, _text):
        self._edited = True

    def setText(self, text):
        self._edited = True
        self.lineEdit().setText(text)

    def setPlaceholderText(self, text):
        self.lineEdit().setPlaceholderText(text)

    def minimum(self):
        return self._minimum

    def maximum(self):
        return self._maximum

    def setRange(self, minimum, maximum):
        self._minimum, self._maximum = minimum, maximum
        self.setValue(self._value)

    def value(self):
        return self._value

    def _display(self, value):
        if value is None:
            return ""
        if value == 0 and self.zero_text:
            return self.zero_text
        if self.unit == "ms" and 0 < value < 1:
            return format_quantity(value * 1000, "µs")
        if self.unit in {"scans", "spectra"}:
            return format_number(value)
        return format_quantity(value, self.unit)

    def setValue(self, value):
        if value is not None:
            value = max(self._minimum, min(self._maximum, value))
            if self.whole:
                value = int(value)
        previous = self._value
        self._value = value
        self._shown = self._display(value)
        self._edited = False
        self.lineEdit().setText(self._shown)
        self._validate_input()
        if value != previous:
            self.valueChanged.emit(value)

    def read_value(self):
        text = self.text().strip()
        if not self._edited and text == self._shown:
            return self._value
        if self.optional and not text:
            return None
        if self.zero_text and text.casefold() == self.zero_text.casefold():
            return 0
        try:
            value = parse_quantity(text, self.unit)
        except ValueError:
            raise ValueError(f"Enter {self.examples}.") from None
        if self.whole and not value.is_integer():
            raise ValueError("Enter a whole number.")
        if not self._minimum <= value <= self._maximum:
            lo = format_quantity(self._minimum, self.unit)
            hi = format_quantity(self._maximum, self.unit)
            raise ValueError(f"Enter a value from {lo} to {hi}.")
        return int(value) if self.whole else value

    def has_valid_input(self):
        return self._valid

    def _validate_input(self, _text=None):
        error = ""
        try:
            self.read_value()
        except ValueError as exc:
            error = str(exc)
        valid = not error
        if self.property("invalid") != (not valid):
            self.setProperty("invalid", not valid)
            self.style().unpolish(self)
            self.style().polish(self)
        tip = f"Enter {self.examples}."
        if valid and self._value is not None:
            value = self._value
            if self.unit == "ms":
                exact = format_quantity(round(value * 1000), "µs")
            else:
                exact = f"{value!s} {self.unit}"
            tip += f" Current setting is {exact}."
        self.lineEdit().setToolTip(error or tip)
        if valid != self._valid:
            self._valid = valid
            self.validityChanged.emit(valid)

    def commit(self):
        try:
            value = self.read_value()
        except ValueError:
            return False
        self.setValue(value)
        return True

    def stepEnabled(self):
        flags = self.StepEnabledFlag
        if self.step is None or self._value is None:
            return flags.StepNone
        result = flags.StepNone
        if self._value < self._maximum:
            result |= flags.StepUpEnabled
        if self._value > self._minimum:
            result |= flags.StepDownEnabled
        return result

    def stepBy(self, steps):
        if self.step is not None and self.commit() and self._value is not None:
            self.setValue(self._value + steps * self.step)
