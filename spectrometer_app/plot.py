"""Live spectrum plot with time-decaying overlays and wavelength readout."""

from collections import deque
import time

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.collections import LineCollection
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.image import AxesImage
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QMenu

from .core import interpolate_flagged_pixels
from .quantities import format_number, format_quantity


VISIBLE_NM = (380.0, 780.0)
VISIBLE_COLORS = (
    (380, "#b19be3"), (440, "#8899ed"), (478, "#69bafd"), (500, "#6bdddc"),
    (535, "#85df99"), (568, "#c7e283"), (592, "#f9c975"), (620, "#f69a73"),
    (680, "#ef858e"), (780, "#c88296"),
)
PLOT_BACKGROUND = "#161b21"
AXIS_LABEL_COLOR = "#c4cbd2"
PLOT_MUTED_COLOR = "#98a2ac"
TRACE_COLOR = "#57deff"
PERSISTENCE_RGB = to_rgb("#8db8e3")
SATURATION_COLOR = "#ff737c"
MARKER_COLOR = "#c5b0ec"
HOT_PIXEL_COLOR = "#e3b866"
COLD_PIXEL_COLOR = "#8fd0c6"


def _visible_colors():
    anchors, colors = zip(*VISIBLE_COLORS, strict=True)
    sample = np.linspace(*VISIBLE_NM, 401)
    channels = np.asarray([to_rgb(color) for color in colors]).T
    return np.column_stack([np.interp(sample, anchors, channel) for channel in channels])[None, :, :]


def _display_counts(frame, interpolate):
    counts = frame.display_counts
    return interpolate_flagged_pixels(counts, frame.pixel_flags) if interpolate else counts


def _validated_bounds(bounds):
    if bounds is None:
        return None
    lower, upper = map(float, bounds)
    if not np.isfinite([lower, upper]).all() or lower >= upper:
        raise ValueError("Wavelength bounds must be finite and strictly increasing.")
    return lower, upper


class SpectrumPlot(FigureCanvasQTAgg):
    cursor_text = Signal(str)

    def __init__(self):
        figure = Figure(facecolor=PLOT_BACKGROUND)
        super().__init__(figure)
        figure.subplots_adjust(left=0.102, bottom=0.12, right=0.975, top=0.966)
        self.axes = figure.add_subplot()
        self.axes.set_facecolor(PLOT_BACKGROUND)
        self.axes.grid(color="#2b333c", linewidth=0.6, alpha=0.7)
        self.axes.set_axisbelow(True)
        self.axes.tick_params(colors=PLOT_MUTED_COLOR, labelsize=9, length=3)
        for spine in self.axes.spines.values():
            spine.set_color("#46525f")
        self.axes.set_xlabel("Wavelength (nm)", color=AXIS_LABEL_COLOR, labelpad=9)
        self.axes.set_ylabel("Intensity (counts)", color=AXIS_LABEL_COLOR, labelpad=10)
        self.axes.ticklabel_format(useOffset=False, axis="both", style="plain")

        self.wavelength_strip = AxesImage(
            self.axes, extent=(*VISIBLE_NM, 0.012, 0.027), interpolation="bilinear",
            transform=self.axes.get_xaxis_transform(), alpha=0.66, zorder=1.7, visible=False,
        )
        self.wavelength_strip.set_data(_visible_colors())
        self.wavelength_strip.set_clip_path(self.axes.patch)
        self.axes.add_image(self.wavelength_strip)
        self.history = LineCollection([], linewidths=0.8, zorder=2)
        self.axes.add_collection(self.history)
        (self.trace,) = self.axes.plot([], [], color=TRACE_COLOR, linewidth=1.3, zorder=3)
        self.saturated_trace = LineCollection([], colors=SATURATION_COLOR, linewidths=2.1, zorder=3.5)
        self.axes.add_collection(self.saturated_trace)
        self.saturated_points = self.axes.scatter([], [], c=SATURATION_COLOR, s=19, linewidths=0, zorder=4.5)
        self.hot = self.axes.scatter([], [], marker="^", c=HOT_PIXEL_COLOR, s=25, zorder=4)
        self.cold = self.axes.scatter([], [], marker="v", c=COLD_PIXEL_COLOR, s=25, zorder=4)
        self.edge_hot = self.axes.scatter(
            [], [], marker="^", c=HOT_PIXEL_COLOR, s=25, zorder=4, transform=self.axes.get_xaxis_transform(),
        )
        self.edge_cold = self.axes.scatter(
            [], [], marker="v", c=COLD_PIXEL_COLOR, s=25, zorder=4, transform=self.axes.get_xaxis_transform(),
        )
        crosshair_style = dict(color=AXIS_LABEL_COLOR, linewidth=0.8, linestyle=(0, (3, 3)), alpha=0.65,
                               zorder=5, visible=False)
        self.crosshair_x = self.axes.axvline(0, **crosshair_style)
        self.crosshair_y = self.axes.axhline(0, **crosshair_style)
        self.marker_lines = LineCollection(
            [], colors=MARKER_COLOR, linewidths=0.9, linestyles=(0, (2, 3)), alpha=0.78,
            transform=self.axes.get_xaxis_transform(), zorder=5.2,
        )
        self.axes.add_collection(self.marker_lines)
        self.marker_points = self.axes.scatter(
            [], [], facecolors="none", edgecolors=MARKER_COLOR, s=34, linewidths=1.15, zorder=5.4,
        )
        self.markers = {}
        self.placeholder = self.axes.text(
            0.5, 0.5, "Connect a spectrometer to see its live spectrum",
            transform=self.axes.transAxes, ha="center", va="center", color=PLOT_MUTED_COLOR,
        )
        self.axes.xaxis.set_visible(False)
        self.axes.yaxis.set_visible(False)
        self.frames = deque(maxlen=160)
        self.latest = None
        self._latest_counts = None
        self.persistence_s = 1
        self.mark_pixels = True
        self.show_pixel_triangles = True
        self.interpolate_pixels = False
        self.auto_scale_y = False
        self.wavelength_colors = False
        self.wavelength_mode = "full"
        self.manual_wavelength_bounds = None
        self.auto_wavelength_bounds = None
        self._follows_bounds = True
        self.drag = None
        self._panning = False
        self._last_marker_click = None
        self.context_menu = QMenu(self)
        self.crosshair_action = self.context_menu.addAction("Crosshair")
        self.crosshair_action.setCheckable(True)
        self.crosshair_action.toggled.connect(self._crosshair_changed)
        self.place_markers_action = self.context_menu.addAction("Click to place markers")
        self.place_markers_action.setCheckable(True)
        self.clear_markers_action = self.context_menu.addAction("Clear markers", self.clear_markers)
        self.context_menu.addSeparator()
        self.show_pixel_triangles_action = self.context_menu.addAction("Show hot/cold triangles")
        self.show_pixel_triangles_action.setCheckable(True)
        self.show_pixel_triangles_action.setChecked(self.show_pixel_triangles)
        self.show_pixel_triangles_action.toggled.connect(self.set_show_pixel_triangles)
        self.auto_scale_y_action = self.context_menu.addAction("Auto Y")
        self.auto_scale_y_action.setCheckable(True)
        self.auto_scale_y_action.toggled.connect(self.set_auto_scale_y)
        self.context_fit_y = self.context_menu.addAction("Fit Y in visible range", self.fit_y)
        self.context_fit_both = self.context_menu.addAction("Fit X and Y", self.fit_both)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.mpl_connect("motion_notify_event", self._motion)
        self.mpl_connect("axes_leave_event", self._leave_axes)
        self.mpl_connect("button_press_event", self._press)
        self.mpl_connect("button_release_event", self._release)
        self.mpl_connect("scroll_event", self._zoom)
        self.mpl_connect("resize_event", lambda _event: self._layout_marker_labels())

    def clear(self):
        self.latest = None
        self._latest_counts = None
        self.auto_wavelength_bounds = None
        self._follows_bounds = True
        self.drag = None
        self._panning = False
        self.frames.clear()
        self.trace.set_data([], [])
        self._draw_saturation()
        self.clear_markers()
        self._leave_axes(None)
        self._draw_flags()
        self._draw_wavelength_colors()
        self.placeholder.set_visible(True)
        self.axes.xaxis.set_visible(False)
        self.axes.yaxis.set_visible(False)
        self.refresh_history()

    def show_frame(self, frame):
        first = self.latest is None
        if not first:
            previous = self.latest
            if (
                previous.integration_us != frame.integration_us
                or previous.scans_to_average != frame.scans_to_average
                or (previous.corrected_counts is None) != (frame.corrected_counts is None)
                or (previous.dark_record and previous.dark_record.profile_id)
                != (frame.dark_record and frame.dark_record.profile_id)
            ):
                self.frames.clear()
        self.latest = frame
        self.placeholder.set_visible(False)
        self.axes.xaxis.set_visible(True)
        self.axes.yaxis.set_visible(True)
        self.axes.set_ylabel(
            "Dark-subtracted intensity (counts)" if frame.corrected_counts is not None
            else "Intensity (counts)",
            color=AXIS_LABEL_COLOR, labelpad=10,
        )
        self._draw_trace()
        self._draw_wavelength_colors()
        if first:
            self.fit_both()
        elif self.auto_scale_y:
            self.fit_y()
        if self.persistence_s:
            self.frames.append(frame)
        self.refresh_history()

    def set_persistence(self, seconds):
        self.persistence_s = seconds
        if not seconds:
            self.frames.clear()
        self.refresh_history()

    def refresh_history(self):
        now = time.monotonic()
        while self.frames and now - self.frames[0].acquired_monotonic > self.persistence_s:
            self.frames.popleft()
        history = [frame for frame in self.frames if frame is not self.latest]
        self.history.set_segments(
            [np.column_stack((frame.wavelengths_nm, _display_counts(frame, self.interpolate_pixels))) for frame in history]
        )
        self.history.set_color([
            (*PERSISTENCE_RGB, 0.36 * (1 - (now - frame.acquired_monotonic) / self.persistence_s))
            for frame in history
        ])
        self.draw_idle()

    def set_mark_pixels(self, enabled):
        self.mark_pixels = enabled
        self._draw_flags()
        self.draw_idle()

    def set_show_pixel_triangles(self, enabled):
        if enabled == self.show_pixel_triangles:
            return
        self.show_pixel_triangles = enabled
        self.show_pixel_triangles_action.setChecked(enabled)
        self._draw_flags()
        self.draw_idle()

    def set_interpolate_pixels(self, enabled):
        self.interpolate_pixels = enabled
        if self.latest is not None:
            self._draw_trace()
            self.fit_y()
        self.refresh_history()
        self.cursor_text.emit("")

    def set_wavelength_colors(self, enabled):
        self.wavelength_colors = enabled
        self._draw_wavelength_colors()
        self.draw_idle()

    def _draw_wavelength_colors(self):
        self.wavelength_strip.set_visible(self.wavelength_colors and self.latest is not None)

    def _draw_trace(self):
        self._latest_counts = _display_counts(self.latest, self.interpolate_pixels)
        self.trace.set_data(self.latest.wavelengths_nm, self._latest_counts)
        self._draw_saturation()
        self._draw_flags()
        self._draw_markers()

    def _draw_saturation(self):
        if self.latest is None:
            self.saturated_trace.set_segments([])
            self.saturated_points.set_offsets(np.empty((0, 2)))
            return
        saturated = self.latest.saturation_mask & np.isfinite(self._latest_counts)
        transitions = np.flatnonzero(np.diff(np.r_[False, saturated, False]))
        points = np.column_stack((self.latest.wavelengths_nm, self._latest_counts))
        runs = [points[start:stop] for start, stop in transitions.reshape(-1, 2)]
        self.saturated_trace.set_segments([run for run in runs if len(run) > 1])
        isolated = [run for run in runs if len(run) == 1]
        self.saturated_points.set_offsets(np.concatenate(isolated) if isolated else np.empty((0, 2)))

    def _draw_flags(self):
        frame = self.latest
        markers = ((self.hot, self.edge_hot, 1), (self.cold, self.edge_cold, -1))
        flags = None if frame is None else frame.pixel_flags
        if not self.mark_pixels or not self.show_pixel_triangles or flags is None:
            for marker, edge_marker, _sign in markers:
                marker.set_offsets(np.empty((0, 2)))
                edge_marker.set_offsets(np.empty((0, 2)))
            return

        finite = np.isfinite(self._latest_counts)
        for marker, edge_marker, sign in markers:
            samples = (flags == sign) & finite
            gaps = frame.wavelengths_nm[(flags == sign) & ~finite]
            marker.set_offsets(np.column_stack((frame.wavelengths_nm[samples], self._latest_counts[samples])))
            edge_marker.set_offsets(np.column_stack((gaps, np.full(gaps.size, 0.055))))

    def set_wavelength_bounds(self, mode, bounds=None):
        if mode == "manual":
            manual = _validated_bounds(bounds) if bounds is not None else self.manual_wavelength_bounds
            if manual is None:
                raise ValueError("Manual wavelength bounds are required.")
            self.manual_wavelength_bounds = manual
        elif mode == "auto" and bounds is not None:
            self.auto_wavelength_bounds = _validated_bounds(bounds)
        elif mode not in {"auto", "full"}:
            raise ValueError(f"Unknown wavelength display mode: {mode!r}.")
        self.wavelength_mode = mode
        self.fit_both()

    def set_auto_wavelength_bounds(self, bounds):
        bounds = _validated_bounds(bounds)
        if bounds == self.auto_wavelength_bounds:
            return
        self.auto_wavelength_bounds = bounds
        if self.wavelength_mode == "auto" and self._follows_bounds:
            self.fit_both()

    def _selected_bounds(self):
        wavelength = self.latest.wavelengths_nm
        lower, upper = float(np.min(wavelength)), float(np.max(wavelength))
        if self.wavelength_mode == "manual":
            return self.manual_wavelength_bounds
        if self.wavelength_mode == "auto" and self.auto_wavelength_bounds is not None:
            auto_lower, auto_upper = self.auto_wavelength_bounds
            clipped = max(lower, auto_lower), min(upper, auto_upper)
            if clipped[0] < clipped[1]:
                return clipped
        return (lower, upper) if lower < upper else (lower - 0.5, upper + 0.5)

    def fit_both(self):
        self._follows_bounds = True
        if self.latest is not None:
            wavelength = self.latest.wavelengths_nm
            lower, upper = self._selected_bounds()
            self.axes.set_xlim((lower, upper) if wavelength[-1] >= wavelength[0] else (upper, lower))
            self._draw_markers()
            self.fit_y()
            self.draw_idle()

    def set_auto_scale_y(self, enabled):
        if enabled == self.auto_scale_y:
            return
        self.auto_scale_y = enabled
        self.auto_scale_y_action.setChecked(enabled)
        if enabled:
            self.fit_y()

    def fit_y(self):
        if self.latest is None:
            return
        xmin, xmax = sorted(self.axes.get_xlim())
        in_view = (self.latest.wavelengths_nm >= xmin) & (self.latest.wavelengths_nm <= xmax)
        values = self._latest_counts[in_view & np.isfinite(self._latest_counts)]
        if values.size:
            lower, upper = float(np.min(values)), float(np.max(values))
            span = max(upper - lower, 1.0)
            padding = 0.07 * span
            headroom = padding
            if self.auto_scale_y:
                # Approach full scale with data-driven limits and keep a small
                # margin for the red plateau once a trace reaches the ceiling.
                headroom = min(headroom, max(self.latest.info.max_counts - upper, 0.02 * span))
            self.axes.set_ylim(lower - padding, upper + headroom)
            self.draw_idle()

    def _show_context_menu(self, position):
        has_trace = self.latest is not None
        self.clear_markers_action.setEnabled(bool(self.markers))
        self.context_fit_y.setEnabled(has_trace)
        self.context_fit_both.setEnabled(has_trace)
        self.context_menu.popup(self.mapToGlobal(position))

    def _crosshair_changed(self, enabled):
        if not enabled:
            self._hide_crosshair()

    def _hide_crosshair(self):
        if self.crosshair_x.get_visible():
            self.crosshair_x.set_visible(False)
            self.crosshair_y.set_visible(False)
            self.draw_idle()

    def _leave_axes(self, _event):
        self._hide_crosshair()
        self.cursor_text.emit("")

    def _channel_at(self, wavelength):
        return int(np.argmin(np.abs(self.latest.wavelengths_nm - wavelength)))

    def _toggle_marker(self, channel):
        previous = self.markers.pop(channel, None)
        if previous is not None:
            previous.remove()
        else:
            self.markers[channel] = self.axes.annotate(
                "", xy=(self.latest.wavelengths_nm[channel], 0.987), xycoords=self.axes.get_xaxis_transform(),
                xytext=(5, -3), textcoords="offset points", ha="left", va="top", fontsize=8, zorder=6,
                color="#e8ebee", bbox=dict(boxstyle="round,pad=0.3", facecolor="#2c2440", edgecolor="#6e5d8e"),
                annotation_clip=True,
            )
        self._draw_markers()
        self.draw_idle()

    def clear_markers(self):
        for label in self.markers.values():
            label.remove()
        self.markers.clear()
        self._last_marker_click = None
        self._draw_markers()
        self.draw_idle()

    def _draw_markers(self):
        points = []
        lines = []
        if self.latest is not None and self.markers:
            for channel, label in self.markers.items():
                wavelength = self.latest.wavelengths_nm[channel]
                counts = self._latest_counts[channel]
                finite = np.isfinite(counts)
                if finite:
                    points.append((wavelength, counts))
                lines.append(((wavelength, 0), (wavelength, 1)))
                value = format_quantity(counts, "counts") if finite else "display gap"
                label.set_text(f"{format_quantity(wavelength, 'nm')} · {value}")
        self.marker_points.set_offsets(np.asarray(points).reshape(-1, 2))
        self.marker_lines.set_segments(lines)
        self._layout_marker_labels()

    def _layout_marker_labels(self):
        if not self.markers:
            return
        renderer = self.get_renderer()
        x_padding = renderer.points_to_pixels(4)
        left, right = self.axes.get_xlim()
        lower, upper = sorted((left, right))
        rows = []
        for label in sorted(self.markers.values(), key=lambda label: label.xy[0], reverse=left > right):
            if not lower <= label.xy[0] <= upper:
                label.set_visible(False)
                continue
            label.set_visible(True)
            offset = 5
            label.set_position((offset, -3))
            label.set_horizontalalignment("left")
            extent = label.get_window_extent(renderer)
            if extent.x1 + x_padding > self.axes.bbox.x1:
                offset = -5
                label.set_position((offset, -3))
                label.set_horizontalalignment("right")
                extent = label.get_window_extent(renderer)
            start, end = extent.x0 - x_padding, extent.x1 + x_padding
            row = next((index for index, spans in enumerate(rows)
                        if all(end <= other_start or start >= other_end for other_start, other_end in spans)), len(rows))
            if row == len(rows):
                rows.append([])
            rows[row].append((start, end))
            label.set_position((offset, -3 - row * 16))

    def _press(self, event):
        if event.button != 1:
            return
        if event.inaxes != self.axes or self.latest is None:
            self._last_marker_click = None
            return
        if event.dblclick:
            if self._last_marker_click is not None:
                self._toggle_marker(self._last_marker_click)
                self._last_marker_click = None
            self.fit_both()
            return
        self._last_marker_click = None
        self.drag = event.x, event.y, self.axes.get_xlim(), self.axes.get_ylim()
        self._panning = False

    def _outside_drag_threshold(self, event):
        origin_x, origin_y = self.drag[:2]
        distance = abs(event.x - origin_x) + abs(event.y - origin_y)
        return distance >= QApplication.startDragDistance() * self.device_pixel_ratio

    def _release(self, event):
        if event.button != 1:
            return
        if (self.drag is not None and not self._panning and not self._outside_drag_threshold(event)
                and event.inaxes == self.axes and event.xdata is not None and self.latest is not None
                and self.place_markers_action.isChecked()):
            channel = self._channel_at(event.xdata)
            self._toggle_marker(channel)
            self._last_marker_click = channel
        self.drag = None
        self._panning = False

    def _motion(self, event):
        if self.drag is not None and (self._panning or self._outside_drag_threshold(event)):
            self._panning = True
            self._follows_bounds = False
            origin_x, origin_y, xlim, ylim = self.drag
            dx = (event.x - origin_x) * (xlim[1] - xlim[0]) / self.axes.bbox.width
            self.axes.set_xlim(xlim[0] - dx, xlim[1] - dx)
            if self.auto_scale_y:
                self.fit_y()
            else:
                dy = (event.y - origin_y) * (ylim[1] - ylim[0]) / self.axes.bbox.height
                self.axes.set_ylim(ylim[0] - dy, ylim[1] - dy)
            self._layout_marker_labels()
            self.draw_idle()
        if event.inaxes == self.axes and event.xdata is not None and self.latest is not None:
            frame = self.latest
            channel = self._channel_at(event.xdata)
            if self.crosshair_action.isChecked():
                self.crosshair_x.set_xdata([event.xdata, event.xdata])
                self.crosshair_y.set_ydata([event.ydata, event.ydata])
                self.crosshair_x.set_visible(True)
                self.crosshair_y.set_visible(True)
                self.draw_idle()
            counts = self._latest_counts[channel]
            flag = 0 if frame.pixel_flags is None else int(frame.pixel_flags[channel])
            value = "display gap" if np.isnan(counts) else format_quantity(counts, "counts")
            detail = ""
            if flag:
                detail = "   hot pixel" if flag > 0 else "   cold pixel"
                if self.interpolate_pixels:
                    if np.isfinite(counts):
                        detail += " · interpolated"
                    detail += f" · measured {format_quantity(frame.display_counts[channel], 'counts')}"
            if frame.saturation_mask[channel]:
                detail += " · raw channel saturated"
            wavelength = format_quantity(frame.wavelengths_nm[channel], "nm")
            self.cursor_text.emit(f"{wavelength}   {value}   pixel {format_number(channel)}{detail}")

    def _zoom(self, event):
        if event.inaxes != self.axes or self.latest is None:
            return
        self._follows_bounds = False
        factor = 1.18 ** -event.step
        self.axes.set_xlim([event.xdata + (bound - event.xdata) * factor for bound in self.axes.get_xlim()])
        if self.auto_scale_y:
            self.fit_y()
        else:
            self.axes.set_ylim([event.ydata + (bound - event.ydata) * factor for bound in self.axes.get_ylim()])
        self._layout_marker_labels()
        self.draw_idle()
