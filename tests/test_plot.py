"""Display-only pixel treatment and wavelength scaling on the Qt canvas."""

from dataclasses import replace
from datetime import datetime, timezone
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from spectrometer_app.core import DarkRecord, Frame, InstrumentInfo
from spectrometer_app.plot import SpectrumPlot


@pytest.fixture
def plot():
    app = QApplication.instance() or QApplication([])
    canvas = SpectrumPlot()
    yield canvas
    canvas.close()
    app.processEvents()


def frame(profile="dark-1"):
    raw = np.array([8000, 15, -6000, 25, 30, 35, -9000], dtype=float)
    wavelengths = np.array([800, 700, 690, 660, 510, 400, 300], dtype=float)
    dark = np.ones_like(raw)
    flags = np.array([1, 0, -1, 0, 0, 0, -1], dtype=np.int8)
    now = datetime.now(timezone.utc)
    elapsed = time.monotonic()
    return Frame(
        info=InstrumentInfo("Maya2000Pro", "test", "demo", 8_000, 3_000_000, 65535),
        wavelengths_nm=wavelengths,
        integration_us=20_000,
        raw_counts=raw,
        started_at=now,
        acquired_at=now,
        started_monotonic=elapsed,
        acquired_monotonic=elapsed,
        dark_record=DarkRecord(20_000, dark, flags, profile, 1, now),
        corrected_counts=raw - dark,
        dark_subtraction_requested=True,
    )


def test_interpolation_changes_only_drawn_counts_and_respects_edges(plot):
    first = frame()
    second = replace(first, acquired_monotonic=time.monotonic())
    plot.show_frame(first)
    plot.show_frame(second)
    np.testing.assert_array_equal(plot.trace.get_ydata(), first.corrected_counts)

    plot.set_interpolate_pixels(True)
    np.testing.assert_allclose(plot.trace.get_ydata(), [np.nan, 14, 19, 24, 29, 34, np.nan])
    np.testing.assert_allclose(plot.history.get_segments()[0][:, 1], [14, 19, 24, 29, 34])
    np.testing.assert_array_equal(second.raw_counts, [8000, 15, -6000, 25, 30, 35, -9000])
    np.testing.assert_array_equal(second.corrected_counts, [7999, 14, -6001, 24, 29, 34, -9001])
    assert 0 < plot.axes.get_ylim()[0] < plot.axes.get_ylim()[1] < 50
    assert plot.edge_hot.get_offsets()[0, 0] == 800
    assert plot.edge_cold.get_offsets()[0, 0] == 300
    np.testing.assert_array_equal(plot.cold.get_offsets(), [[690, 19]])

    messages = []
    plot.cursor_text.connect(messages.append)
    plot._motion(SimpleNamespace(inaxes=plot.axes, xdata=690))
    assert "19 counts" in messages[-1] and "interpolated · measured -6,001 counts" in messages[-1]
    plot._motion(SimpleNamespace(inaxes=plot.axes, xdata=800))
    assert "display gap" in messages[-1] and "hot pixel" in messages[-1]

    plot.set_mark_pixels(False)
    assert plot.edge_hot.get_offsets().size == 0 and plot.cold.get_offsets().size == 0
    plot.set_interpolate_pixels(False)
    np.testing.assert_array_equal(plot.trace.get_ydata(), second.corrected_counts)


def test_wavelength_modes_history_profile_break_and_color_footprint(plot):
    first = frame()
    plot.set_wavelength_colors(True)
    assert not plot.wavelength_strip.get_visible()
    plot.set_wavelength_bounds("auto")
    plot.set_auto_wavelength_bounds((391, 745))
    plot.show_frame(first)
    assert plot.axes.get_xlim() == (745, 391)
    assert plot.wavelength_strip.get_visible()
    assert plot.wavelength_strip.get_extent()[:2] == (380, 780)
    plot.draw()

    plot._zoom(SimpleNamespace(inaxes=plot.axes, step=1, xdata=600, ydata=12))
    zoomed = plot.axes.get_xlim()
    plot.set_auto_wavelength_bounds((440, 700))
    assert plot.axes.get_xlim() == zoomed
    plot.fit_both()
    assert plot.axes.get_xlim() == (700, 440)
    plot.set_wavelength_bounds("manual", (480, 670))
    assert plot.axes.get_xlim() == (670, 480)
    plot.fit_y()
    assert 20 < plot.axes.get_ylim()[0] < plot.axes.get_ylim()[1] < 31
    plot.set_wavelength_bounds("full")
    assert plot.axes.get_xlim() == (800, 300)
    plot.draw()
    colored = np.asarray(plot.buffer_rgba()).copy()
    plot.set_wavelength_colors(False)
    plot.draw()
    plain = np.asarray(plot.buffer_rgba())
    row = colored.shape[0] - 1 - int(plot.axes.bbox.y0 + 0.019 * plot.axes.bbox.height)
    visible_x = int(plot.axes.transData.transform((500, 0))[0])
    near_ir_x = int(plot.axes.transData.transform((790, 0))[0])
    assert np.any(colored[row, visible_x] != plain[row, visible_x])
    np.testing.assert_array_equal(colored[row, near_ir_x], plain[row, near_ir_x])
    plot.set_wavelength_colors(True)

    plot.show_frame(replace(first, acquired_monotonic=time.monotonic()))
    assert len(plot.history.get_segments()) == 1
    plot.show_frame(frame(profile="dark-2"))
    assert len(plot.history.get_segments()) == 0
    plot.clear()
    assert plot.auto_wavelength_bounds is None and not plot.wavelength_strip.get_visible()


def test_saturation_uses_raw_full_scale_and_tracks_visible_trace(plot):
    first = frame()
    raw = np.array([4095, 4100, 3915, 4095, 1100, 4094, 20], dtype=float)
    flags = np.array([1, 0, 0, 1, 0, 0, 0], dtype=np.int8)
    dark = np.full(raw.size, 100.0)
    clipped = replace(
        first, info=replace(first.info, max_counts=4095), raw_counts=raw, corrected_counts=raw - dark,
        dark_record=replace(first.dark_record, counts=dark, pixel_flags=flags),
    )
    plot.show_frame(clipped)
    assert plot.axes.get_xlim() == (800, 300)
    assert clipped.saturated_pixels == 3
    np.testing.assert_array_equal(plot.saturated_trace.get_segments()[0], [[800, 3995], [700, 4000]])
    np.testing.assert_array_equal(plot.saturated_points.get_offsets(), [[660, 3995]])
    plot.draw()
    pixels = np.asarray(plot.buffer_rgba())
    x, y = np.rint(plot.axes.transData.transform((750, 3997.5))).astype(int)
    row = pixels.shape[0] - 1 - y
    trace = pixels[row - 3:row + 4, x - 3:x + 4, :3]
    assert np.any((trace[..., 0] > 220) & (trace[..., 1] < 136) & (trace[..., 2] < 136))

    plot.set_interpolate_pixels(True)
    assert not plot.saturated_trace.get_segments()
    np.testing.assert_array_equal(plot.saturated_points.get_offsets(), [[700, 4000], [660, 2407.5]])
    np.testing.assert_array_equal(clipped.raw_counts, raw)
    np.testing.assert_array_equal(clipped.corrected_counts, raw - dark)

    messages = []
    plot.cursor_text.connect(messages.append)
    plot._motion(SimpleNamespace(inaxes=plot.axes, xdata=660, ydata=2400))
    assert "raw channel saturated" in messages[-1]
    plot.show_frame(replace(clipped, raw_counts=raw - 100, corrected_counts=raw - 200))
    assert plot.saturated_points.get_offsets().size == 0


def test_context_menu_crosshair_markers_click_and_pan(plot):
    first = frame()
    plot.show_frame(first)
    plot.show()
    position = QPoint(90, 90)
    QApplication.sendEvent(plot, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, plot.mapToGlobal(position)))
    assert plot.context_menu.isVisible()
    assert not plot.clear_markers_action.isEnabled()
    plot.context_menu.hide()

    event = dict(inaxes=plot.axes, xdata=510, ydata=29, x=90, y=90, button=1, dblclick=False)
    plot.crosshair_action.trigger()
    plot._motion(SimpleNamespace(**event))
    assert plot.crosshair_x.get_visible() and plot.crosshair_y.get_visible()
    assert plot.crosshair_x.get_xdata() == [510, 510]
    assert plot.crosshair_y.get_ydata() == [29, 29]
    plot._leave_axes(None)
    assert not plot.crosshair_x.get_visible() and not plot.crosshair_y.get_visible()

    plot.place_markers_action.trigger()
    plot.draw()
    x, y = plot.axes.transData.transform((510, 29)) / plot.device_pixel_ratio
    QTest.mouseClick(plot, Qt.MouseButton.LeftButton, pos=QPoint(round(x), round(plot.height() - y)))
    assert list(plot.markers) == [4]
    assert plot.markers[4].get_text() == "510 nm · 29 counts"
    np.testing.assert_array_equal(plot.marker_lines.get_segments()[0], [[510, 0], [510, 1]])
    np.testing.assert_array_equal(plot.marker_points.get_offsets(), [[510, 29]])

    plot.show_frame(replace(first, corrected_counts=first.corrected_counts + 5))
    assert plot.markers[4].get_text() == "510 nm · 34 counts"
    np.testing.assert_array_equal(plot.marker_points.get_offsets(), [[510, 34]])

    before = plot.axes.get_xlim()
    moved = SimpleNamespace(**(event | {"x": 135, "y": 125, "xdata": 640}))
    plot._press(SimpleNamespace(**event))
    plot._motion(moved)
    plot._release(moved)
    assert plot.axes.get_xlim() != before and list(plot.markers) == [4]

    other = SimpleNamespace(**(event | {"xdata": 660}))
    plot._press(other)
    plot._release(other)
    assert list(plot.markers) == [4, 3]
    plot._press(SimpleNamespace(**(event | {"xdata": 660, "dblclick": True})))
    assert plot.axes.get_xlim() == before and list(plot.markers) == [4]

    plot._show_context_menu(position)
    assert plot.clear_markers_action.isEnabled()
    plot.clear_markers_action.trigger()
    plot.context_menu.hide()
    assert not plot.markers and not plot.marker_lines.get_segments()


def test_overlapping_marker_labels_stagger_and_reflow(plot):
    plot.show_frame(frame())
    for channel in (1, 2, 5):
        plot._toggle_marker(channel)
    plot.draw()
    first, second, distant = (plot.markers[index] for index in (1, 2, 5))
    assert first.get_position()[1] == distant.get_position()[1]
    assert second.get_position()[1] < first.get_position()[1]
    assert not first.get_bbox_patch().get_window_extent().overlaps(second.get_bbox_patch().get_window_extent())

    plot.set_wavelength_bounds("manual", (675, 715))
    plot.draw()
    assert not distant.get_visible()
    assert first.get_position()[1] == second.get_position()[1]
    assert not first.get_bbox_patch().get_window_extent().overlaps(second.get_bbox_patch().get_window_extent())


def test_pixel_triangles_can_be_hidden_with_interpolation_still_active(plot):
    plot.show_frame(frame())
    plot.set_interpolate_pixels(True)
    before = plot.trace.get_ydata().copy()
    assert plot.show_pixel_triangles_action.isChecked()
    assert plot.cold.get_offsets().size and plot.edge_hot.get_offsets().size

    plot.set_show_pixel_triangles(False)
    assert not plot.show_pixel_triangles_action.isChecked()
    assert not any(marker.get_offsets().size for marker in (plot.hot, plot.cold, plot.edge_hot, plot.edge_cold))
    np.testing.assert_array_equal(plot.trace.get_ydata(), before)
    messages = []
    plot.cursor_text.connect(messages.append)
    plot._motion(SimpleNamespace(inaxes=plot.axes, xdata=690))
    assert "cold pixel · interpolated" in messages[-1]

    plot.show_pixel_triangles_action.trigger()
    assert plot.cold.get_offsets().size and plot.edge_hot.get_offsets().size


def test_live_auto_y_follows_signal_and_preserves_wavelength_limits(plot):
    original = frame()
    dark = np.full(original.raw_counts.size, 50.0)
    info = replace(original.info, max_counts=4095)
    bright_counts = np.array([4095, 10, 4095, 4095, 450, 124, 10], dtype=float)
    faint_counts = np.array([4095, 10, 109, 224, 450, 124, 10], dtype=float)
    bright = replace(original, info=info, raw_counts=bright_counts, corrected_counts=bright_counts - dark)
    faint = replace(original, info=info, raw_counts=faint_counts, corrected_counts=faint_counts - dark)
    plot.set_wavelength_bounds("manual", (390, 710))
    plot.show_frame(faint)
    x_limits = plot.axes.get_xlim()
    manual = plot.axes.get_ylim()
    assert not plot.auto_scale_y_action.isChecked() and manual[0] < -40 < 400 < manual[1] < 500
    plot.show_frame(bright)
    assert plot.axes.get_ylim() == manual

    plot.auto_scale_y_action.trigger()
    bright_limits = plot.axes.get_ylim()
    assert bright_limits[0] < -40 and 4045 < bright_limits[1] < 4200
    assert plot.axes.get_xlim() == x_limits
    plot.show_frame(faint)
    assert plot.axes.get_ylim() == manual
    assert plot.axes.get_xlim() == x_limits
    plot.refresh_history()
    assert plot.axes.get_ylim() == manual

    plot.set_auto_scale_y(False)
    assert not plot.auto_scale_y_action.isChecked()
    plot.show_frame(bright)
    assert plot.axes.get_ylim() == manual
    plot.fit_y()
    assert plot.axes.get_ylim()[1] > 4045


def test_persistence_starts_at_one_second_and_resets_on_averaging_change(plot):
    first = frame()
    assert plot.persistence_s == 1
    plot.show_frame(first)
    plot.show_frame(replace(first, acquired_monotonic=time.monotonic()))
    assert len(plot.history.get_segments()) == 1
    plot.show_frame(replace(first, scans_to_average=3, acquired_monotonic=time.monotonic()))
    assert not plot.history.get_segments()
