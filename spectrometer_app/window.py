"""Desktop controls and presentation for live wavelength spectra."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import numpy as np
from matplotlib import get_data_path
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFontDatabase, QIcon, QKeySequence, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStyle, QStyleOptionButton, QVBoxLayout, QWidget,
)

from . import APP_NAME, __version__
from .core import MAX_SCANS_TO_AVERAGE
from .dialogs import DarkLibrariesDialog, DarkSessionDialog, WavelengthRangeDialog, local_date
from .fields import QuantityField
from .plot import SpectrumPlot
from .quantities import format_exposure, format_number, format_quantity
from .updates import UpdateController
from .worker import AcquisitionWorker, Capture, DarkInventory


DEFAULT_SERIES_COUNT = 10
DEFAULT_SERIES_INTERVAL_S = 0.5


STYLE = """
QWidget { background: #12161b; color: #e8ebee; font: 10pt "Segoe UI"; }
QMainWindow { background: #12161b; }
QMenuBar { background: #12161b; border-bottom: 1px solid #2b333c; padding: 4px 7px; }
QMenuBar::item { padding: 5px 11px; }
QMenu, QComboBox QAbstractItemView { background: #1a1f26; border: 1px solid #3a4652; }
QMenu::item { padding: 7px 32px 7px 20px; }
QMenu::item:selected, QMenuBar::item:selected, QComboBox QAbstractItemView::item:selected {
  background: #1e2c3a; color: #a9cbed;
}
QMenu::item:disabled { color: #79838e; }
QWidget#sidebar, QWidget#controls { background: #1a1f26; }
QWidget#controls QLabel, QWidget#controls QCheckBox { background: transparent; }
QScrollArea { border: none; background: #1a1f26; }
QScrollBar:vertical { width: 9px; margin: 0; background: #1a1f26; }
QScrollBar::handle:vertical { min-height: 26px; background: #475564; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QFrame#divider { color: #2b333c; }
QLabel[role="title"] { font-size: 21pt; font-weight: 600; }
QLabel[role="section"] { font-weight: 600; font-size: 10.5pt; color: #e8ebee; }
QLabel[role="quiet"] { color: #98a2ac; font-size: 9pt; }
QLabel[role="live"] { color: #8fd1a5; font-weight: 600; }
QLabel[role="warn"] { color: #e3b866; font-weight: 600; }
QLabel[role="saturated"] {
  color: #f0a1a8; background: #3c2023; border: 1px solid #8a4c55;
  border-radius: 6px; font-weight: 600; padding: 3px 8px;
}
QPushButton, QLineEdit, QComboBox, QAbstractSpinBox {
  border: 1px solid #3a4652; border-radius: 7px; padding: 5px 8px;
  min-height: 21px; background: #232a32; selection-background-color: #8db8e3;
  selection-color: #12161b;
}
QPushButton { padding: 5px 9px; }
QPushButton:hover { background: #293746; border-color: #74879a; }
QPushButton:pressed { background: #344556; }
QPushButton:checked { background: #1e2c3a; border-color: #8db8e3; color: #a9cbed; }
QPushButton:disabled, QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {
  background: #1a1f26; color: #818b96; border-color: #2b333c;
}
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
  border-color: #8db8e3;
}
QAbstractSpinBox QLineEdit { border: none; background: transparent; padding: 0; min-height: 0; }
QAbstractSpinBox[invalid="true"] { border-color: #f0a1a8; }
QPushButton[role="primary"] {
  background: #8fd1a5; border-color: #8fd1a5; color: #14251b; font-weight: 600;
}
QPushButton[role="primary"]:hover { background: #abe0bb; border-color: #abe0bb; }
QPushButton[role="primary"]:disabled { background: #1d3326; border-color: #3a5344; color: #9eb6a7; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator {
  width: 13px; height: 13px; background: #232a32; border: 1px solid #657382; border-radius: 4px;
}
QCheckBox::indicator:checked {
  background: #8db8e3; border: 1px solid #8db8e3;
}
QCheckBox::indicator:disabled {
  border-color: #3a4652;
}
QCheckBox::indicator:checked:disabled {
  background: #6985a1; border-color: #6985a1;
}
QPushButton[role="stepper"] { padding: 0; }
QProgressBar { background: #232a32; border: 1px solid #3a4652; border-radius: 4px; }
QProgressBar::chunk { background: #8db8e3; border-radius: 3px; }
QFrame#plotFrame { background: #161b21; border: 1px solid #2b333c; border-radius: 8px; }
QStatusBar { border-top: 1px solid #2b333c; color: #c4cbd2; font-size: 9pt; }
QToolTip { background: #263342; color: #e8ebee; border: 1px solid #51637a; }
"""


def action_icon(kind, color="#c4cbd2"):
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if kind == "save":
        painter.drawLine(10, 3, 10, 12)
        painter.drawLine(7, 9, 10, 12)
        painter.drawLine(10, 12, 13, 9)
        painter.drawPolyline([QPoint(x, y) for x, y in ((4, 12), (4, 16), (16, 16), (16, 12))])
    elif kind == "repeat":
        painter.drawPolyline([QPoint(x, y) for x, y in ((4, 8), (4, 5), (16, 5), (13, 2))])
        painter.drawLine(16, 5, 13, 8)
        painter.drawPolyline([QPoint(x, y) for x, y in ((16, 12), (16, 15), (4, 15), (7, 18))])
        painter.drawLine(4, 15, 7, 12)
    elif kind == "play":
        painter.drawPolygon([QPoint(x, y) for x, y in ((6, 4), (15, 10), (6, 16))])
    elif kind == "pause":
        painter.drawLine(7, 4, 7, 16)
        painter.drawLine(13, 4, 13, 16)
    elif kind in ("up", "down"):
        center, outside = (7, 12) if kind == "up" else (12, 7)
        painter.drawPolyline([QPoint(5, outside), QPoint(10, center), QPoint(15, outside)])
    else:
        painter.drawLine(10, 3, 10, 17)
        for x, y in ((7, 6), (13, 6), (7, 14), (13, 14)):
            painter.drawLine(x, y, 10, 3 if y == 6 else 17)
        if kind == "fit_both":
            painter.drawPolyline([QPoint(x, y) for x, y in ((5, 7), (2, 10), (5, 13))])
            painter.drawPolyline([QPoint(x, y) for x, y in ((15, 7), (18, 10), (15, 13))])
    painter.end()
    return QIcon(pixmap)


def label(text, role="quiet", *, wrap=False):
    result = QLabel(text)
    result.setProperty("role", role)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(wrap)
    if wrap:
        result.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return result


def row(*widgets):
    layout = QHBoxLayout()
    layout.setSpacing(7)
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def update_role(widget, role):
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class CheckedBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        center = QPointF(indicator.center())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#152538"), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline([center + QPointF(-3.7, 0), center + QPointF(-1.0, 2.5), center + QPointF(4, -3)])
        painter.end()


class Requests(QObject):
    scan = Signal()
    connect_device = Signal(object)
    disconnect = Signal()
    live = Signal(bool)
    integration = Signal(int)
    averages = Signal(int)
    subtract = Signal(bool)
    auto = Signal()
    capture = Signal(object)
    cancel_capture = Signal()
    dark = Signal(object)
    select_dark = Signal(object)
    cancel_dark = Signal()
    shutdown = Signal()


class SpectrumWindow(QMainWindow):
    def __init__(self, data_directory: Path, demo: str | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 850)
        self.setMinimumSize(900, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        stylesheet = STYLE
        if "Segoe UI" not in QFontDatabase.families():
            fonts = Path(get_data_path()) / "fonts" / "ttf"
            registered = QFontDatabase.addApplicationFont(str(fonts / "DejaVuSans.ttf"))
            if registered >= 0:
                QFontDatabase.addApplicationFont(str(fonts / "DejaVuSans-Bold.ttf"))
                stylesheet = stylesheet.replace("Segoe UI", "DejaVu Sans")
        self.setStyleSheet(stylesheet)
        self.data_directory = data_directory
        self.directory = data_directory / "Spectra"
        self.info = None
        self.armed = None
        self.is_live = False
        self.capturing = False
        self.calibrating = False
        self.connecting = False
        self.closing = False
        self.warned_darks = set()
        self.auto_pending = False
        self.last_saved = None
        self.dialogs = set()
        self.dark_inventory = DarkInventory((), None, None, None)
        self.range_mode = "auto"
        self.requests = Requests(self)
        self.updates = UpdateController(self)

        self._build_ui()
        self._build_menus()
        self._start_worker()
        self._update_controls()
        self.statusBar().showMessage("Choose an instrument and connect.")
        if demo is not None:
            self.devices.addItem(f"Demo · {demo}", demo)
            self._toggle_device()
        else:
            self._scan()
            self.updates.start()

        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(230)
        self.plot_timer.timeout.connect(self._age_plot)
        self.plot_timer.start()

    def _build_ui(self):
        shell = QWidget()
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_spectrum(), 1)
        self.setCentralWidget(shell)

    def _build_sidebar(self):
        sidebar = QScrollArea()
        self.sidebar = sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(334)
        sidebar.setWidgetResizable(True)
        sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        contents = QWidget()
        contents.setObjectName("controls")
        sidebar.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(18, 19, 18, 16)
        layout.setSpacing(8)

        layout.addWidget(label("Instrument", "section"))
        self.devices = QComboBox()
        self.devices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.devices.setMinimumContentsLength(22)
        self.devices.setAccessibleName("Spectrometer")
        self.devices.setToolTip("Select a detected USB spectrometer.")
        self.devices.currentIndexChanged.connect(lambda _index: self._update_controls())
        layout.addWidget(self.devices)
        self.scan_button = QPushButton("Rescan")
        self.scan_button.setToolTip("Refresh the list after plugging in an instrument.")
        self.scan_button.clicked.connect(self._scan)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_device)
        layout.addLayout(row(self.scan_button, self.connect_button))
        self.identity = label("", wrap=True)
        layout.addWidget(self.identity)

        self._section(layout, "Acquisition")
        self.integration = QuantityField("ms", 0.001, 10_000, 100, examples="80 ms, 0.08 s, or 8e4 µs", step=10)
        self.integration.setAccessibleName("Integration time")
        self.integration.setToolTip("Set a manual exposure. Exact multiples of 10 ms can match a saved dark.")
        self.integration.valueChanged.connect(self._set_integration)
        self.integration.validityChanged.connect(self._update_controls)
        self.auto_button = QPushButton("Auto exposure")
        self.auto_button.setToolTip(
            "Aim for 70% of full scale. Uses exact 10 ms settings and follows the selected dark library when available."
        )
        self.auto_button.clicked.connect(self._auto)
        layout.addWidget(label("Integration time"))
        layout.addLayout(row(self.integration, self.auto_button))
        self.averages = QuantityField(
            "scans", 1, MAX_SCANS_TO_AVERAGE, 1,
            examples=f"1 through {MAX_SCANS_TO_AVERAGE} scans", whole=True,
        )
        self.averages.setFixedWidth(76)
        self.averages.setAccessibleName("Scans per spectrum")
        self.averages.setToolTip(
            "Average this many consecutive scans at the same integration time. 1 displays and saves a single scan."
        )
        self.averages.valueChanged.connect(self._set_averages)
        self.averages.validityChanged.connect(self._update_controls)
        averaging_row = row(label("Scans per spectrum"))
        averaging_row.addStretch()
        averaging_row.addWidget(self.averages)
        layout.addLayout(averaging_row)

        self._section(layout, "Dark reference")
        self.subtract = CheckedBox("Subtract matching dark")
        self.subtract.setToolTip("Apply the exact matching dark to the display and the corrected CSV.")
        self.subtract.toggled.connect(self._change_subtraction)
        layout.addWidget(self.subtract)
        self.dark_status = label("Connect an instrument", wrap=True)
        self.dark_status.setToolTip("An exact match requires the same detector, wavelength calibration, and integration time.")
        layout.addWidget(self.dark_status)
        self.dark_profile_label = label("", wrap=True)
        self.dark_profile_label.hide()
        layout.addWidget(self.dark_profile_label)
        self.dark_button = QPushButton("Collect dark library…")
        self.dark_button.clicked.connect(self._toggle_dark)
        layout.addWidget(self.dark_button)

        self._section(layout, "Display")
        self.persistence = QuantityField("s", 0, 10, 1, examples="1 s, 1,000 ms, or Off", whole=True, zero_text="Off")
        self.persistence.setFixedWidth(54)
        self.persistence.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.persistence.setAccessibleName("Trace persistence")
        persistence_tip = "Overlay fading recent traces for 1–10 seconds. Off hides previous traces."
        self.persistence.setToolTip(persistence_tip)
        persistence_label = label("Trace persistence")
        persistence_label.setToolTip(persistence_tip)
        persistence = row(persistence_label)
        persistence.addStretch()
        persistence.addWidget(self.persistence)
        self.persistence_down = self._persistence_button("down", "Decrease trace persistence", self.persistence.stepDown)
        self.persistence_up = self._persistence_button("up", "Increase trace persistence", self.persistence.stepUp)
        persistence.addWidget(self.persistence_down)
        persistence.addWidget(self.persistence_up)
        self.persistence.valueChanged.connect(self._update_persistence_buttons)
        self._update_persistence_buttons(self.persistence.value())
        layout.addLayout(persistence)
        self.pixels = CheckedBox("Mark hot and cold pixels")
        self.pixels.setToolTip("Identify suspect pixels from the exact matching dark. Hide triangles in the plot’s right-click menu.")
        self.pixels.setChecked(True)
        layout.addWidget(self.pixels)
        self.interpolate = CheckedBox("Interpolate marked pixels")
        self.interpolate.setToolTip(
            "Bridge flagged channels using their neighbors in the display and corrected CSV. The raw CSV keeps the measured counts."
        )
        self.interpolate.setChecked(True)
        nested = row(self.interpolate)
        nested.setContentsMargins(20, 0, 0, 0)
        layout.addLayout(nested)

        self._section(layout, "Save spectra")
        self.title = QLineEdit()
        self.title.setPlaceholderText("Measurement title")
        self.title.setAccessibleName("Measurement title")
        self.title.setToolTip("Use this title in new CSV filenames and measurement metadata.")
        self.title.setText("Spectrum")
        self.title.textChanged.connect(self._update_controls)
        layout.addWidget(self.title)
        self.location = QPushButton("Choose folder…")
        self.location.setToolTip("Choose where the app writes new spectrum CSV files.")
        self.location.clicked.connect(self._choose_directory)
        self.path_label = label("", wrap=True)
        self._show_output_folder()
        layout.addWidget(self.location)
        layout.addWidget(self.path_label)
        self.count = QuantityField("spectra", 2, 9999, DEFAULT_SERIES_COUNT, examples="10 spectra or 1e2", whole=True)
        self.count.setAccessibleName("Repeated capture count")
        self.count.setToolTip("Number of new spectra to save in an armed series.")
        self.interval = QuantityField(
            "s", 0.1, 86_400, DEFAULT_SERIES_INTERVAL_S, examples="0.5 s, 500 ms, or 0.5 min"
        )
        self.interval.setAccessibleName("Repeated capture interval")
        self.interval.setToolTip("Minimum time between the first scans of successive saved spectra.")
        self.count.validityChanged.connect(self._update_controls)
        self.interval.validityChanged.connect(self._update_controls)
        series = QHBoxLayout()
        series.setSpacing(7)
        for field, name in ((self.count, "Spectra in series"), (self.interval, "Interval")):
            group = QVBoxLayout()
            group.setSpacing(3)
            group.addWidget(label(name))
            group.addWidget(field)
            series.addLayout(group, 1)
        layout.addLayout(series)
        self.capture_status = label("", wrap=True)
        self.capture_status.hide()
        layout.addWidget(self.capture_status)
        self.start_button = QPushButton("Start measurements")
        self.start_button.setProperty("role", "primary")
        self.start_button.setIcon(action_icon("play", "#14251b"))
        self.start_button.setToolTip("Start saving the named measurement armed above.")
        self.start_button.clicked.connect(self._start_armed)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Disarm this measurement or stop saving the remaining series.")
        self.cancel_button.clicked.connect(self._cancel_capture)
        self.cancel_button.hide()
        layout.addLayout(row(self.start_button, self.cancel_button))
        layout.addStretch()
        return sidebar

    @staticmethod
    def _persistence_button(direction, description, callback):
        button = QPushButton()
        button.setProperty("role", "stepper")
        button.setFixedSize(32, 33)
        button.setIcon(action_icon(direction))
        button.setAutoRepeat(True)
        button.setAccessibleName(description)
        button.setToolTip(f"{description} by one second.")
        button.clicked.connect(callback)
        return button

    def _update_persistence_buttons(self, seconds):
        self.persistence_down.setEnabled(seconds > self.persistence.minimum())
        self.persistence_up.setEnabled(seconds < self.persistence.maximum())

    @staticmethod
    def _section(layout, title):
        layout.addSpacing(3)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)
        layout.addWidget(label(title, "section"))

    def _build_spectrum(self):
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(22, 16, 22, 12)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_area = QVBoxLayout()
        title_area.setSpacing(2)
        title_area.addWidget(label("Spectrum", "title"))
        self.subtitle = label("No instrument connected")
        title_area.addWidget(self.subtitle)
        header.addLayout(title_area, 1)
        self.live_state = label("Disconnected")
        self.live_state.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.saturation_status = label("", "saturated")
        self.saturation_status.setAccessibleName("Detector full-scale warning")
        self.saturation_status.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.saturation_status.hide()
        live_area = QVBoxLayout()
        live_area.setSpacing(4)
        live_area.addWidget(self.live_state, 0, Qt.AlignmentFlag.AlignRight)
        live_area.addWidget(self.saturation_status, 0, Qt.AlignmentFlag.AlignRight)
        self.dark_progress_group = QWidget()
        self.dark_progress_group.hide()
        self.dark_progress = QProgressBar()
        self.dark_progress.setFixedSize(92, 9)
        self.dark_progress.setTextVisible(False)
        self.dark_progress.setAccessibleName("Dark session progress")
        self.dark_progress_text = label("")
        progress_row = row(self.dark_progress, self.dark_progress_text)
        progress_row.setContentsMargins(0, 0, 0, 0)
        self.dark_progress_group.setLayout(progress_row)
        live_area.addWidget(self.dark_progress_group, 0, Qt.AlignmentFlag.AlignRight)
        header.addLayout(live_area)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(7)
        self.live_button = QPushButton("Resume")
        self.live_button.setToolTip("Pause on the last displayed trace or resume acquiring live spectra.")
        self.live_button.setIcon(action_icon("play"))
        self.live_button.clicked.connect(self._toggle_live)
        self.save_button = QPushButton("Save next")
        self.save_button.setIcon(action_icon("save"))
        self.save_button.setToolTip("Save raw and corrected CSV files from the next exposure.")
        self.save_button.clicked.connect(self._save_next)
        self.repeat_button = QPushButton("Repeat save")
        self.repeat_button.setIcon(action_icon("repeat"))
        self.repeat_button.setToolTip("Arm a series. Start it from the Save spectra panel.")
        self.repeat_button.clicked.connect(self._arm_repeat)
        for button in (self.live_button, self.save_button, self.repeat_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        self.auto_y_button = QPushButton("Auto Y")
        self.auto_y_button.setCheckable(True)
        self.auto_y_button.setToolTip("Fit the vertical scale to each new spectrum within the visible wavelength range.")
        self.fit_y_button = QPushButton("Fit Y")
        self.fit_y_button.setIcon(action_icon("fit_y"))
        self.fit_y_button.setToolTip("Fit counts within the visible wavelength range.")
        self.fit_both_button = QPushButton("Fit X/Y")
        self.fit_both_button.setIcon(action_icon("fit_both"))
        self.fit_both_button.setToolTip("Restore the wavelength range selected in View and fit counts within it.")
        toolbar.addWidget(self.auto_y_button)
        toolbar.addWidget(self.fit_y_button)
        toolbar.addWidget(self.fit_both_button)
        layout.addLayout(toolbar)

        plot_frame = QFrame()
        plot_frame.setObjectName("plotFrame")
        plot_layout = QVBoxLayout(plot_frame)
        plot_layout.setContentsMargins(3, 3, 3, 3)
        self.plot = SpectrumPlot()
        self.plot.set_wavelength_bounds(self.range_mode)
        self.plot.setToolTip("Scroll to zoom, drag to pan, double-click to fit, or right-click for plot tools.")
        plot_layout.addWidget(self.plot)
        layout.addWidget(plot_frame, 1)
        self.fit_y_button.clicked.connect(self.plot.fit_y)
        self.fit_both_button.clicked.connect(self.plot.fit_both)
        self.auto_y_button.toggled.connect(self.plot.auto_scale_y_action.setChecked)
        self.plot.auto_scale_y_action.toggled.connect(self.auto_y_button.setChecked)
        self.persistence.valueChanged.connect(self.plot.set_persistence)
        self.pixels.toggled.connect(self._mark_pixels)
        self.interpolate.toggled.connect(
            lambda checked: self.plot.set_interpolate_pixels(checked and self.pixels.isChecked())
        )
        self._mark_pixels(self.pixels.isChecked())

        footer = QHBoxLayout()
        self.trace_status = label("")
        self.cursor_status = label("")
        self.cursor_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.plot.cursor_text.connect(self.cursor_status.setText)
        footer.addWidget(self.trace_status, 1)
        footer.addWidget(self.cursor_status)
        layout.addLayout(footer)
        return area

    def _mark_pixels(self, checked):
        self.plot.set_mark_pixels(checked)
        self.interpolate.setVisible(checked)
        if checked:
            self.interpolate.setChecked(True)
        self.plot.set_interpolate_pixels(checked and self.interpolate.isChecked())

    def _add_action(self, menu, title, callback, shortcut=None):
        action = QAction(title, self)
        action.triggered.connect(callback)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        menu.addAction(action)
        return action

    def _build_menus(self):
        file = self.menuBar().addMenu("&File")
        self.save_action = self._add_action(file, "Save next spectrum", self._save_next, "Ctrl+S")
        self.arm_action = self._add_action(file, "Arm single spectrum", self._arm_single)
        self.repeat_action = self._add_action(file, "Arm repeated save", self._arm_repeat, "Ctrl+Shift+S")
        self.start_action = self._add_action(file, "Start armed measurements", self._start_armed, "Ctrl+Return")
        file.addSeparator()
        self._add_action(file, "Choose output folder…", self._choose_directory)
        self._add_action(file, "Open output folder", self._open_directory)
        file.addSeparator()
        self._add_action(file, "Exit", self.close, "Ctrl+Q")
        edit = self.menuBar().addMenu("&Edit")
        self._add_action(edit, "Edit measurement title", self._focus_title, "Ctrl+L")
        self._add_action(edit, "Reset series to 10 spectra every 0.5 s", self._reset_series)
        view = self.menuBar().addMenu("&View")
        view.setToolTipsVisible(True)
        self.live_action = self._add_action(view, "Pause or resume live view", self._toggle_live, "Space")
        self.fit_y_action = self._add_action(view, "Fit Y in visible range", self.plot.fit_y, "Y")
        self.fit_both_action = self._add_action(view, "Fit X and Y", self.plot.fit_both, "Home")
        view.addAction(self.plot.auto_scale_y_action)
        wavelengths = view.addMenu("Wavelength range")
        wavelengths.setToolTipsVisible(True)
        self.range_group = QActionGroup(self)
        self.range_actions = {
            "auto": self._add_action(wavelengths, "Auto wavelength range", lambda: self._set_range("auto")),
            "manual": self._add_action(wavelengths, "Set wavelength edges…", self._choose_wavelength_edges),
            "full": self._add_action(wavelengths, "Full detector readout", lambda: self._set_range("full")),
        }
        for mode, action in self.range_actions.items():
            action.setCheckable(True)
            self.range_group.addAction(action)
            action.setChecked(mode == self.range_mode)
        self.range_actions["auto"].setToolTip("Use documented spectral channels and supported dark-derived limits.")
        self.range_actions["manual"].setToolTip("Enter the lower and upper visible wavelengths in nanometers.")
        self.range_actions["full"].setToolTip("Show all returned channels, including detector edges and padding.")
        self.color_action = QAction("Wavelength color guide", self)
        self.color_action.setCheckable(True)
        self.color_action.setToolTip("Show an approximate color strip for visible wavelengths at the base of the plot.")
        self.color_action.toggled.connect(self.plot.set_wavelength_colors)
        view.addAction(self.color_action)
        view.addAction(self.plot.show_pixel_triangles_action)
        devices = self.menuBar().addMenu("&Instrument")
        devices.setToolTipsVisible(True)
        self.scan_action = self._add_action(devices, "Rescan USB devices", self._scan, "F5")
        self.dark_libraries_action = self._add_action(devices, "Dark libraries…", self._choose_dark_library)
        self.dark_libraries_action.setToolTip("Review and reuse completed dark libraries for this instrument.")
        demo = devices.addMenu("Open simulated instrument")
        self.demo_actions = [
            self._add_action(demo, model, lambda checked=False, model=model: self._open_demo(model))
            for model in ("Maya2000Pro", "USB650")
        ]
        help_menu = self.menuBar().addMenu("&Help")
        self._add_action(help_menu, "Plot controls", self._plot_help)
        help_menu.addAction(self.updates.check_action)
        help_menu.addAction(self.updates.automatic_action)
        self._add_action(help_menu, f"About {APP_NAME}", self._about)

    def _start_worker(self):
        self.thread = QThread(self)
        self.worker = AcquisitionWorker(self.data_directory / "Darks")
        self.worker.moveToThread(self.thread)
        for signal, slot in (
            (self.requests.scan, self.worker.scan),
            (self.requests.connect_device, self.worker.connect_device),
            (self.requests.disconnect, self.worker.disconnect),
            (self.requests.live, self.worker.set_live),
            (self.requests.integration, self.worker.set_integration),
            (self.requests.averages, self.worker.set_averaging),
            (self.requests.subtract, self.worker.set_subtraction),
            (self.requests.auto, self.worker.auto_expose),
            (self.requests.capture, self.worker.start_capture),
            (self.requests.cancel_capture, self.worker.cancel_capture),
            (self.requests.dark, self.worker.start_dark),
            (self.requests.select_dark, self.worker.select_dark_library),
            (self.requests.cancel_dark, self.worker._cancel_dark),
            (self.requests.shutdown, self.worker.shutdown),
            (self.worker.inventory, self._show_inventory),
            (self.worker.connected, self._device_opened),
            (self.worker.disconnected, self._device_closed),
            (self.worker.frame_ready, self._show_frame),
            (self.worker.live_changed, self._live_changed),
            (self.worker.capture_changed, self._capture_changed),
            (self.worker.dark_changed, self._dark_changed),
            (self.worker.dark_progress, self._show_dark_progress),
            (self.worker.dark_inventory, self._show_dark_inventory),
            (self.worker.dark_failed, self._dark_failed),
            (self.worker.exposure_changed, self._exposure_changed),
            (self.worker.auto_finished, self._auto_finished),
            (self.worker.error, self._error),
            (self.worker.saved, self._saved),
            (self.worker.finished, self.thread.quit),
            (self.thread.finished, self.worker.deleteLater),
            (self.thread.finished, self.close),
        ):
            signal.connect(slot)
        self.thread.start()

    def _scan(self):
        if self.info is None and not self.connecting:
            self.connecting = True
            self._update_controls()
            self.devices.clear()
            self.devices.addItem("Searching USB devices…")
            self.requests.scan.emit()

    @Slot(object)
    def _show_inventory(self, devices):
        self.connecting = False
        self.devices.clear()
        for device in devices:
            self.devices.addItem(device.label, device)
        if not devices:
            self.devices.addItem("No supported USB spectrometer")
            self.statusBar().showMessage("Connect a Maya2000 Pro or USB650, then rescan.")
        else:
            self.statusBar().showMessage("Select a spectrometer and connect.")
        self._update_controls()

    def _open_demo(self, model):
        if self.info is None and not self.connecting:
            self.devices.addItem(f"Demo · {model}", model)
            self.devices.setCurrentIndex(self.devices.count() - 1)
            self._toggle_device()

    def _toggle_device(self):
        if self.connecting:
            return
        if self.info is not None:
            self.connecting = True
            self._update_controls()
            self.worker.request_immediate_read_stop()
            self.requests.disconnect.emit()
            return
        selection = self.devices.currentData()
        if selection is not None:
            self.connecting = True
            self._update_controls()
            self.requests.connect_device.emit(selection)

    @Slot(object)
    def _device_opened(self, info):
        self.connecting = False
        self.info = info
        self.integration.blockSignals(True)
        self.integration.setRange(info.min_integration_us / 1000, info.max_integration_us / 1000)
        self.integration.blockSignals(False)
        self.identity.setText(f"{info.serial} · {info.backend}")
        self.subtitle.setText(f"{info.model}  ·  {info.serial}")
        self.dark_status.setText("Checking integration time…")
        self.warned_darks.clear()
        self.statusBar().showMessage("Spectrometer connected.", 3500)
        self._update_controls()

    @Slot()
    def _device_closed(self):
        self.connecting = False
        self.info = None
        self.armed = None
        self.capturing = self.calibrating = self.is_live = self.auto_pending = False
        self.identity.clear()
        self.subtitle.setText("No instrument connected")
        self.live_state.setText("Disconnected")
        update_role(self.live_state, "quiet")
        self.saturation_status.hide()
        self.dark_status.setText("Connect an instrument")
        self.dark_inventory = DarkInventory((), None, None, None)
        self.dark_profile_label.hide()
        self.dark_progress_group.hide()
        self.trace_status.clear()
        self.capture_status.hide()
        self.plot.clear()
        self._set_range("auto")
        self._update_controls()

    def _set_integration(self, milliseconds):
        if self.info is not None:
            self.auto_pending = False
            self.warned_darks.clear()
            self.dark_status.setText("Checking integration time…")
            if self.is_live:
                self.worker.request_immediate_read_stop()
            self.requests.integration.emit(round(milliseconds * 1000))
            if not self.is_live:
                self.statusBar().showMessage("Resume live view to check the dark reference.")

    def _set_averages(self, scans):
        if self.is_live:
            self.worker.request_immediate_read_stop()
        self.requests.averages.emit(scans)

    @Slot(int)
    def _exposure_changed(self, microseconds):
        self.integration.blockSignals(True)
        self.integration.setValue(microseconds / 1000)
        self.integration.blockSignals(False)

    def _change_subtraction(self, checked):
        self.warned_darks.clear()
        self.requests.subtract.emit(checked)
        if self.info is not None and not self.is_live:
            self.statusBar().showMessage("Resume live view to update the trace.")

    def _auto(self):
        if not self.integration.commit() or not self.averages.commit():
            return
        self.warned_darks.clear()
        self.auto_pending = True
        self._update_controls()
        if self.is_live:
            self.worker.request_immediate_read_stop()
        self.requests.auto.emit()
        self.statusBar().showMessage("Adjusting integration time…")

    @Slot()
    def _auto_finished(self):
        self.auto_pending = False
        self.statusBar().showMessage("Auto exposure finished.", 3500)
        self._update_controls()

    def _toggle_live(self):
        if self.info is not None and not (self.capturing or self.calibrating or self.connecting):
            if self.is_live:
                self.worker.request_immediate_read_stop()
            self.requests.live.emit(not self.is_live)

    @Slot(bool)
    def _live_changed(self, live):
        self.is_live = live
        self.live_button.setText("Pause" if live else "Resume")
        self.live_button.setIcon(action_icon("pause" if live else "play"))
        self._update_controls()

    @Slot(object)
    def _show_frame(self, frame):
        if self.info is None or self.calibrating or self.closing:
            return
        previous = self.plot.latest
        self.plot.show_frame(frame)
        if previous is not None and (
            previous.integration_us != frame.integration_us
            or (previous.corrected_counts is None) != (frame.corrected_counts is None)
        ):
            self.plot.fit_y()
        flags = frame.pixel_flags
        hot = int(np.count_nonzero(flags == 1)) if flags is not None else 0
        cold = int(np.count_nonzero(flags == -1)) if flags is not None else 0
        exposure = format_exposure(frame.integration_us)
        if frame.scans_to_average > 1:
            exposure += f"/scan  ·  {frame.scans_to_average}-scan mean"
        status = f"{exposure}  ·  {frame.raw_counts.size:,} readout channels"
        if flags is not None:
            status += f"  ·  {hot} hot / {cold} cold"
        self.trace_status.setText(status)
        self._show_saturation(frame)
        if int(round(self.integration.value() * 1000)) == frame.integration_us:
            self._show_dark_match(frame)
        self._plot_controls()

    def _show_saturation(self, frame):
        saturated = frame.saturated_pixels
        if not saturated:
            self.saturation_status.hide()
            return
        self.saturation_status.setText(f"Full-scale warning · {format_number(saturated)} channels")
        self.saturation_status.setToolTip(
            f"{format_number(saturated)} readout channels reached the backend’s "
            f"{format_quantity(frame.info.max_counts, 'raw counts')} threshold in "
            f"{frame.saturated_scans} of {frame.scans_to_average} scans. "
            "Red highlights those channels on the displayed trace."
        )
        self.saturation_status.show()

    def _show_dark_match(self, frame):
        if frame.dark_record is not None:
            word = "Subtraction active" if frame.corrected_counts is not None else "Matching dark available"
            self.dark_status.setText(f"{word} · {format_exposure(frame.integration_us)}")
            update_role(self.dark_status, "quiet")
            return
        requested = self.subtract.isChecked()
        microseconds = frame.integration_us
        on_grid = 10_000 <= microseconds <= 3_000_000 and microseconds % 10_000 == 0
        exposure = format_exposure(microseconds, exact=True)
        self.dark_status.setText(f"No matching dark · {exposure}")
        update_role(self.dark_status, "warn" if requested or not on_grid else "quiet")
        if (
            (requested or not on_grid) and microseconds not in self.warned_darks
            and not self.auto_pending and not self.capturing
        ):
            self.warned_darks.add(microseconds)
            self._notice(
                "No saved dark at this integration time",
                f"There is no matching dark at {exposure} for this detector. "
                "The plot shows raw counts. The dark library covers supported exact "
                "10 ms steps from 10 to 3,000 ms.",
            )

    def _age_plot(self):
        if len(self.plot.frames) > 1:
            self.plot.refresh_history()

    def _set_range(self, mode, bounds=None):
        self.range_mode = mode
        self.plot.set_wavelength_bounds(mode, bounds)
        self.range_actions[mode].setChecked(True)

    def _choose_wavelength_edges(self):
        frame = self.plot.latest
        if frame is not None:
            full_bounds = float(np.min(frame.wavelengths_nm)), float(np.max(frame.wavelengths_nm))
            shown_bounds = tuple(sorted(self.plot.axes.get_xlim()))
            dialog = WavelengthRangeDialog(self, full_bounds, shown_bounds)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._set_range("manual", dialog.bounds)
        self.range_actions[self.range_mode].setChecked(True)

    def _choose_directory(self):
        if self.capturing:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Choose spectrum output folder", str(self.directory))
        if chosen:
            self.directory = Path(chosen)
            self._show_output_folder()

    def _show_output_folder(self):
        subpath = Path(*self.directory.parts[-2:])
        self.path_label.setText(str(subpath))
        self.path_label.setToolTip(str(self.directory))

    def _open_directory(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.startfile(self.directory)
        except (OSError, AttributeError) as exc:
            self._error("Open output folder", str(exc))

    def _reset_series(self):
        if not self.capturing and self.armed is None:
            self.count.setValue(DEFAULT_SERIES_COUNT)
            self.interval.setValue(DEFAULT_SERIES_INTERVAL_S)

    def _arm_single(self):
        if self.integration.commit() and self.averages.commit():
            self._arm(1, 0)

    def _arm_repeat(self):
        if self.integration.commit() and self.averages.commit() and self.count.commit() and self.interval.commit():
            self._arm(self.count.value(), self.interval.value())

    def _arm(self, count, interval):
        if self.info is None or self.capturing or self.calibrating or self.connecting:
            return
        title = self.title.text().strip()
        if not title:
            self.title.setFocus()
            return
        self.armed = Capture(self.directory, title, count, interval)
        cadence = f"{format_number(count)} spectra · every {format_quantity(interval, 's')}" if count > 1 else "1 spectrum"
        self.capture_status.setText(f"Armed · {title}\n{cadence}")
        self.capture_status.show()
        self._update_controls()
        QTimer.singleShot(0, self, lambda: self.sidebar.ensureWidgetVisible(self.start_button))

    def _save_next(self):
        if self.armed is None:
            self._arm_single()
            self._start_armed()

    def _start_armed(self):
        if self.armed is None or self.capturing or self.calibrating or self.connecting:
            return
        if not self.integration.commit() or not self.averages.commit():
            return
        scans = self.averages.value()
        nominal_s = scans * self.integration.value() / 1000
        if self.armed.count > 1 and nominal_s >= self.armed.interval_s:
            self.statusBar().showMessage(
                f"Each spectrum uses {format_quantity(nominal_s, 's')} of programmed integration. "
                "Actual spacing may exceed the requested interval.",
                8000,
            )
        self.capturing = True
        self.capture_status.setText(f"Starting · {self.armed.title}")
        self.worker.request_immediate_read_stop()
        self.requests.capture.emit(replace(
            self.armed, interpolate_pixels=self.pixels.isChecked() and self.interpolate.isChecked(),
            scans_to_average=scans,
        ))
        self.armed = None
        self._update_controls()

    def _cancel_capture(self):
        if self.capturing:
            self.worker.request_immediate_read_stop()
            self.requests.cancel_capture.emit()
        else:
            self.armed = None
            self.capture_status.hide()
            self._update_controls()

    @Slot(str, bool)
    def _capture_changed(self, status, active):
        self.capturing = active
        self.capture_status.setText(status)
        self.capture_status.setVisible(bool(status))
        self._update_controls()

    @Slot(object)
    def _saved(self, files):
        self.last_saved = files
        companion = "identity companion" if files.corrected.name.endswith("_corrected_identity.csv") else "corrected CSV"
        self.statusBar().showMessage(f"Saved raw CSV and {companion}", 5000)

    def _toggle_dark(self):
        if self.calibrating:
            self.worker.request_immediate_dark_cancel()
            self.requests.cancel_dark.emit()
            return
        if self.info is None or self.capturing or self.armed is not None:
            return
        dialog = DarkSessionDialog(self, simulated="demo" in self.info.backend.casefold())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.calibrating = True
            self._update_controls()
            if self.is_live:
                self.worker.request_immediate_read_stop()
            self.requests.dark.emit(dialog.options())

    def _choose_dark_library(self):
        state = self.dark_inventory
        active_id = state.active.profile_id if state.active is not None else None
        dialog = DarkLibrariesDialog(self, state.profiles, state.selected_id, active_id)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.requests.select_dark.emit(dialog.selected_id)

    @Slot(str, bool)
    def _dark_changed(self, status, active):
        self.calibrating = active
        self.dark_status.setText(status)
        update_role(self.dark_status, "quiet")
        if not active:
            self.dark_progress_group.hide()
            self.warned_darks.clear()
            self.statusBar().showMessage(status, 12_000)
        self._update_controls()

    @Slot(object)
    def _show_dark_progress(self, progress):
        if not self.calibrating:
            return
        self.dark_progress.setRange(0, round(progress.requested_s * 1000))
        self.dark_progress.setValue(round((progress.requested_s - progress.remaining_s) * 1000))
        self.dark_progress_text.setText(
            f"{format_number(progress.done)}/{format_number(progress.total)} scans · "
            f"{format_exposure(progress.integration_us)}"
        )
        minutes, seconds = divmod(round(progress.remaining_s), 60)
        tool_tip = (
            f"Progress is weighted by exposure duration. Remaining programmed exposure time is "
            f"{minutes} min {seconds:02d} s. Detector settling and USB reads add time."
        )
        if "demo" in self.info.backend.casefold():
            tool_tip = "Progress is weighted by programmed exposure. Simulated reads are accelerated."
        self.dark_progress.setToolTip(tool_tip)
        self.dark_progress_text.setToolTip(tool_tip)
        self.dark_progress_group.show()

    @Slot(object)
    def _show_dark_inventory(self, state):
        if self.info is None:
            return
        previous_active = self.dark_inventory.active
        self.dark_inventory = state
        profile = state.active
        if profile is not None:
            date = local_date(profile.published_at)
            self.dark_profile_label.setText(f"Saved dark · {profile.label or date}")
            temperature = "Temperature not recorded" if profile.temperature_c is None else (
                f"{format_quantity(profile.temperature_c, '°C')} ({profile.temperature_source})"
            )
            self.dark_profile_label.setToolTip(
                f"Saved {date}. {temperature}. Reuse or select libraries in Instrument → Dark libraries."
            )
        self.dark_profile_label.setVisible(profile is not None)
        bounds = state.usable_bounds
        self.plot.set_auto_wavelength_bounds(bounds.bounds_nm if bounds is not None else None)
        self.range_actions["auto"].setToolTip(
            bounds.reason if bounds is not None else "Use the full readout when no supported trim is available."
        )
        if profile != previous_active:
            self.warned_darks.clear()
            if self.plot.latest is not None and not self.is_live and not self.calibrating:
                self.dark_status.setText("Resume to apply the selected dark")

    @Slot(str)
    def _dark_failed(self, detail):
        self._error("Read dark library", detail)

    def _plot_help(self):
        self._notice(
            "Plot controls",
            "Scroll to zoom around the cursor, drag to pan, or double-click to fit both axes. "
            "Fit Y uses the current wavelength range. Auto Y refits the vertical scale on new spectra. "
            "Hover to read the nearest pixel. "
            "Upward amber triangles mark hot pixels. Downward teal triangles mark cold pixels. "
            "The flags come from a matching dark library. Red shows channels that reached the backend’s full scale. "
            "Right-click the plot to toggle the crosshair or place wavelength markers. "
            "Use View to change the wavelength edges or display the wavelength color guide.",
            warning=False,
        )

    def _about(self):
        self._notice(
            f"About {APP_NAME}",
            f"{APP_NAME} {__version__}\n\n"
            "Live acquisition for Ocean Optics Maya2000 Pro and USB650 on Windows. "
            "Hardware access uses SeaBreeze. Demo instruments produce synthetic spectra.\n\n"
            "The Windows package uses Qt and Qt for Python under LGPLv3. "
            "See _internal/THIRD-PARTY-NOTICES.txt in the installation folder.",
            warning=False,
        )

    @Slot(str, str)
    def _error(self, title, detail):
        if not self.closing:
            if title == "Auto exposure":
                self.auto_pending = False
            self.statusBar().showMessage(title, 8000)
            self._notice(title, detail)

    def _notice(self, title, text, *, warning=True):
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(text)
        dialog.setIcon(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        self.dialogs.add(dialog)
        dialog.finished.connect(lambda _result: (self.dialogs.discard(dialog), dialog.deleteLater()))
        dialog.open()

    def _plot_controls(self):
        has_trace = self.plot.latest is not None
        for control in (
            self.fit_y_button, self.fit_both_button, self.fit_y_action, self.fit_both_action,
            *self.range_actions.values(),
        ):
            control.setEnabled(has_trace)

    def _focus_title(self):
        self.title.setFocus()
        self.title.selectAll()

    def _update_controls(self):
        if not hasattr(self, "save_action"):
            return
        available = self.info is not None and not self.connecting
        busy = self.capturing or self.calibrating
        armed = self.armed is not None
        editable = available and not busy and not armed
        acquisition_valid = self.integration.has_valid_input() and self.averages.has_valid_input()
        can_save = editable and acquisition_valid and bool(self.title.text().strip())
        can_repeat = can_save and self.count.has_valid_input() and self.interval.has_valid_input()
        scan_ready = not available and not self.connecting
        for control in (self.scan_button, self.scan_action, self.devices, *self.demo_actions):
            control.setEnabled(scan_ready)
        self.connect_button.setText("Disconnect" if available else "Connect")
        self.connect_button.setToolTip(
            "Release the USB instrument." if available else "Open the selected instrument and start live acquisition."
        )
        self.connect_button.setEnabled(
            not self.connecting and not busy and (available or self.devices.currentData() is not None)
        )
        for control in (self.live_button, self.live_action):
            control.setEnabled(available and not busy)
        for control in (self.save_button, self.save_action, self.arm_action):
            control.setEnabled(can_save)
        for control in (self.repeat_button, self.repeat_action):
            control.setEnabled(can_repeat)
        for control in (self.start_button, self.start_action):
            control.setEnabled(available and armed and not busy and acquisition_valid)
        self.cancel_button.setVisible(armed or self.capturing)
        self.integration.setEnabled(available and not busy)
        self.averages.setEnabled(available and not busy)
        self.subtract.setEnabled(available and not busy)
        self.pixels.setEnabled(not self.capturing)
        self.interpolate.setEnabled(not self.capturing)
        self.auto_button.setEnabled(editable and acquisition_valid and not self.auto_pending)
        self.dark_libraries_action.setEnabled(editable)
        self.dark_button.setEnabled(available and (editable or self.calibrating))
        self.dark_button.setText("Cancel dark session" if self.calibrating else "Collect dark library…")
        self.dark_button.setToolTip(
            "Cancel the sweep and retain previously completed libraries." if self.calibrating
            else "With the fiber covered, collect and save a reusable dark library."
        )
        for control in (self.title, self.location, self.count, self.interval):
            control.setEnabled(not busy and not armed)
        if available:
            text = "Dark session" if self.calibrating else "Live" if self.is_live else "Paused"
            if "demo" in self.info.backend.casefold():
                text = f"Simulation · {text}"
            self.live_state.setText(text)
            update_role(self.live_state, "live" if self.is_live and not self.calibrating else "quiet")
        self._plot_controls()

    def closeEvent(self, event):
        self.updates.shutdown()
        if self.thread.isRunning():
            event.ignore()
            if not self.closing:
                self.closing = True
                self.setEnabled(False)
                self.statusBar().showMessage("Closing the spectrometer after the current exposure…")
                self.worker.request_immediate_close()
                self.requests.shutdown.emit()
        else:
            event.accept()
