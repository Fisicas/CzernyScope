"""Less frequently used acquisition and display settings."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from .fields import QuantityField
from .quantities import format_number, format_quantity


TEMPERATURE_NOTE = (
    "Detector temperature can change dark current. Reuse a library collected under comparable "
    "conditions. Ambient temperature is a record of room conditions."
)


@dataclass(frozen=True)
class DarkOptions:
    averages: int
    label: str | None
    temperature_c: float | None
    temperature_source: str | None


def local_date(timestamp):
    return timestamp.astimezone().strftime("%b %d, %Y %I:%M %p")


def body(text):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def buttons(dialog, verb):
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    box.button(QDialogButtonBox.StandardButton.Ok).setText(verb)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


class DarkSessionDialog(QDialog):
    def __init__(self, parent, *, simulated):
        super().__init__(parent)
        self.setWindowTitle("Collect dark library")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(body(
            "Cover the fiber completely before collecting. Keep it covered until the session finishes. "
            "The app saves a completed library for future measurements."
        ))
        fields = QFormLayout()
        self.averages = QuantityField("scans", 1, 5, 1, examples="1 through 5 scans", whole=True)
        self.averages.setToolTip("Average this many independent scans at each supported integration time.")
        fields.addRow("Scans per integration", self.averages)
        self.label = QLineEdit()
        self.label.setMaxLength(80)
        self.label.setPlaceholderText("Optional")
        self.label.setToolTip("Name this library so you can recognize its conditions later.")
        fields.addRow("Library label", self.label)
        self.temperature = QuantityField(
            "°C", -273.15, 250, None, examples="21.5 °C, 68 °F, or 293.15 K",
            step=None, optional=True,
        )
        self.temperature.setPlaceholderText("Optional, if measured")
        self.temperature.setToolTip("Record a measured temperature. The app stores it in degrees Celsius.")
        fields.addRow("Temperature (°C)", self.temperature)
        self.source = QComboBox()
        self.source.addItem("Detector or instrument", "instrument")
        self.source.addItem("Ambient", "ambient")
        self.source.setEnabled(False)
        self.source.setToolTip("Identify what your temperature measurement describes.")
        fields.addRow("Temperature source", self.source)
        layout.addLayout(fields)
        self.timing = body("")
        layout.addWidget(self.timing)
        layout.addWidget(body(TEMPERATURE_NOTE))
        self.buttons = buttons(self, "Collect")
        layout.addWidget(self.buttons)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.temperature.lineEdit().textChanged.connect(self._validate_fields)
        self.averages.validityChanged.connect(self._validate_fields)
        self.averages.valueChanged.connect(self._update_timing)
        self.simulated = simulated
        self._update_timing()

    def _validate_fields(self, _value=None):
        has_temperature = bool(self.temperature.text().strip())
        self.source.setEnabled(has_temperature)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.temperature.has_valid_input() and self.averages.has_valid_input()
        )

    def accept(self):
        if self.temperature.commit() and self.averages.commit():
            super().accept()

    def _update_timing(self):
        seconds = round(self.averages.value() * 451.5)
        minutes, seconds = divmod(seconds, 60)
        detail = "The simulated instrument accelerates these exposures." if self.simulated else (
            f"The full grid requires {minutes} min {seconds:02d} s of exposures, plus detector settling."
        )
        self.timing.setText("Supported grid: 10 to 3,000 ms in 10 ms steps. " + detail)

    def options(self):
        degrees = self.temperature.value()
        return DarkOptions(
            self.averages.value(), self.label.text().strip() or None, degrees,
            self.source.currentData() if degrees is not None else None,
        )


class DarkLibrariesDialog(QDialog):
    def __init__(self, parent, profiles, selected_id, active_id):
        super().__init__(parent)
        self.setWindowTitle("Dark libraries")
        self.setMinimumWidth(475)
        self.profiles = {profile.profile_id: profile for profile in profiles}
        self.latest = max(profiles, key=lambda item: item.published_at, default=None)
        layout = QVBoxLayout(self)
        self.profile = QComboBox()
        self.profile.setAccessibleName("Saved dark library")
        self.profile.setToolTip("Select a saved library for this instrument. Your choice is remembered.")
        self.profile.addItem("Latest completed library", None)
        for profile in profiles:
            name = local_date(profile.published_at)
            if profile.label:
                name = f"{profile.label} · {name}"
            if profile.profile_id == active_id:
                name += " · In use"
            self.profile.addItem(name, profile.profile_id)
        if selected_id is not None:
            index = self.profile.findData(selected_id)
            self.profile.setCurrentIndex(max(index, 0))
        layout.addWidget(self.profile)
        self.summary = body("")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        layout.addWidget(body(TEMPERATURE_NOTE))
        self.buttons = buttons(self, "Use library")
        layout.addWidget(self.buttons)
        self.profile.currentIndexChanged.connect(self._describe)
        self._describe()

    def _describe(self, _index=0):
        selected = self.profile.currentData()
        profile = self.profiles.get(selected) if selected else self.latest
        if profile is None:
            self.summary.setText("There are no complete dark libraries for this detector.")
            return
        times = profile.integration_us
        temperature = "Temperature not recorded"
        if profile.temperature_c is not None:
            temperature = f"{format_quantity(profile.temperature_c, '°C')} ({profile.temperature_source})"
        first, last = (format_number(value / 1000) for value in (times[0], times[-1]))
        self.summary.setText(
            f"Saved {local_date(profile.published_at)}\n"
            f"{format_number(len(times))} exposure settings · {first}–{last} ms · "
            f"{profile.averages} scans per setting\n{temperature}"
        )

    @property
    def selected_id(self):
        return self.profile.currentData()


class WavelengthRangeDialog(QDialog):
    def __init__(self, parent, full_bounds, shown_bounds):
        super().__init__(parent)
        self.setWindowTitle("Set wavelength edges")
        layout = QVBoxLayout(self)
        layout.addWidget(body("Enter the plot limits in nanometers. Saved spectra retain the full detector readout."))
        fields = QFormLayout()
        self.lower, self.upper = (
            QuantityField("nm", *full_bounds, initial, examples="500 nm, 0.5 µm, or 5,000 Å")
            for initial in shown_bounds
        )
        fields.addRow("Lower edge", self.lower)
        fields.addRow("Upper edge", self.upper)
        layout.addLayout(fields)
        self.buttons = buttons(self, "Apply range")
        layout.addWidget(self.buttons)
        self.lower.lineEdit().textChanged.connect(self._validate)
        self.upper.lineEdit().textChanged.connect(self._validate)
        self._validate()

    def _ordered_edges(self):
        try:
            return self.lower.read_value() < self.upper.read_value()
        except ValueError:
            return False

    def _validate(self, _text=None):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self._ordered_edges())

    def accept(self):
        if self._ordered_edges():
            self.lower.commit()
            self.upper.commit()
            super().accept()

    @property
    def bounds(self):
        return self.lower.value(), self.upper.value()
