"""Render a meridional ray schematic of a generic crossed Czerny–Turner bench."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch


PANEL = "#161b21"
TEXT = "#e8ebee"
MUTED = "#aebbc8"
NEUTRAL = "#c4cbd2"
OPTIC = "#d0dbe7"
WAVELENGTH_COLORS = {450: "#92b9f0", 550: "#8fd1a5", 650: "#f0a1a8"}
PERIOD_NM = 1e6 / 600
COLLIMATOR_HALF_WIDTH = 0.40
GRATING_HALF_WIDTH = 0.59
CAMERA_HALF_WIDTH = 0.72


def unit(vector):
    return vector / np.linalg.norm(vector)


def perpendicular(vector):
    return np.array([-vector[1], vector[0]])


def reflect(incident, normal):
    return incident - 2 * np.dot(incident, normal) * normal


def intersect_plane(start, direction, point, normal):
    distance = np.dot(point - start, normal) / np.dot(direction, normal)
    if distance <= 0:
        raise ValueError("Optical-plane intersection is behind the ray.")
    return start + distance * direction


class Mirror:
    def __init__(self, vertex, incident, outgoing, focal_distance):
        self.vertex = vertex
        self.normal = unit(outgoing - incident)
        self.tangent = perpendicular(self.normal)
        # Tangential paraxial focus of an obliquely used spherical mirror.
        self.radius = 2 * focal_distance / abs(np.dot(incident, self.normal))
        self.center = vertex + self.radius * self.normal
        np.testing.assert_allclose(reflect(incident, self.normal), outgoing, atol=1e-12)

    def surface(self, offset):
        return self.center - np.sqrt(self.radius**2 - offset**2) * self.normal + offset * self.tangent

    def intercept(self, start, direction):
        relative = start - self.center
        projection = np.dot(relative, direction)
        root = np.sqrt(projection**2 + self.radius**2 - np.dot(relative, relative))
        points = [start + distance * direction for distance in (-projection - root, -projection + root)
                  if distance > 0]
        return min(points, key=lambda point: np.linalg.norm(point - self.vertex))

    def reflection_at(self, point, incident):
        return reflect(incident, unit(self.center - point))


def trace_paths():
    slit = np.array([1.65, 1.38])
    detector = np.array([-1.65, 1.38])
    grating = np.array([0.0, 0.0])
    left, right = np.array([[-2.0, np.sqrt(12)], [2.0, np.sqrt(12)]])
    tilt = np.deg2rad(11)
    tangent = np.array([np.cos(tilt), np.sin(tilt)])
    normal = perpendicular(tangent)

    collimator = Mirror(left, unit(left - slit), unit(grating - left), np.linalg.norm(left - slit))
    detector_normal = unit(detector - right)
    camera = Mirror(right, unit(right - grating), detector_normal, np.linalg.norm(detector - right))

    input_paths = []
    spectra = {wavelength: [] for wavelength in WAVELENGTH_COLORS}
    for offset in (-0.23, 0.0, 0.23):
        first = collimator.surface(offset)
        incident = collimator.reflection_at(first, unit(first - slit))
        ruled = intersect_plane(first, incident, grating, normal)
        input_paths.append(np.array([slit, first, ruled]))
        assert np.dot(incident, normal) < 0
        for wavelength, paths in spectra.items():
            # First order. The incident arm points away from the grating in the angle convention.
            tangential = np.dot(incident, tangent) + wavelength / PERIOD_NM
            dispersed = tangential * tangent + np.sqrt(1 - tangential**2) * normal
            assert np.dot(dispersed, normal) > 0
            np.testing.assert_allclose(
                PERIOD_NM * np.dot(dispersed - incident, tangent), wavelength, atol=1e-9
            )
            second = camera.intercept(ruled, dispersed)
            focused = camera.reflection_at(second, dispersed)
            image = intersect_plane(second, focused, detector, detector_normal)
            paths.append(np.array([ruled, second, image]))

    return slit, detector, detector_normal, grating, tangent, collimator, camera, input_paths, spectra


def verify_paths(detector, detector_normal, grating_tangent, camera, input_paths, spectra):
    detector_axis = perpendicular(detector_normal)
    hits = np.array([[np.dot(path[-1] - detector, detector_axis) for path in paths] for paths in spectra.values()])
    # Both outer rays stay near their chief ray, and the three images remain distinct.
    separation = np.diff(hits[:, 1])
    assert np.all(separation > 0) and np.ptp(hits, axis=1).max() < separation.min()
    assert max(abs(np.dot(path[-1], grating_tangent)) for path in input_paths) < GRATING_HALF_WIDTH
    assert all(
        abs(np.dot(path[1] - camera.vertex, camera.tangent)) < CAMERA_HALF_WIDTH
        for paths in spectra.values() for path in paths
    )

    # The entrance and camera chief-ray paths cross between their endpoints.
    start, end = input_paths[1][:2]
    mirror, image = spectra[550][1][1:]
    fraction = np.linalg.solve(np.column_stack((end - start, mirror - image)), mirror - start)
    assert np.all((0 < fraction) & (fraction < 1))


def draw_ray(ax, points, *, color, chief=False):
    ax.plot(*points.T, color=color, alpha=0.94 if chief else 0.31,
            linewidth=1.9 if chief else 1.1, solid_capstyle="round", zorder=2)


def arrow(ax, start, stop, color, fraction):
    vector = stop - start
    point = start + fraction * vector
    ax.add_patch(FancyArrowPatch(
        point - 0.10 * unit(vector), point + 0.09 * unit(vector),
        arrowstyle="-|>", mutation_scale=12, color=color, linewidth=1.05, zorder=3,
    ))


def render(destination):
    slit, detector, detector_normal, grating, tangent, collimator, camera, inputs, spectra = trace_paths()
    verify_paths(detector, detector_normal, tangent, camera, inputs, spectra)

    mpl.rcParams.update({"svg.hashsalt": "czernyscope-crossed", "font.family": "DejaVu Sans"})
    figure = Figure(figsize=(11.8, 7.25), facecolor=PANEL)
    FigureCanvasAgg(figure)
    ax = figure.add_axes([0.018, 0.032, 0.964, 0.936])
    ax.set(xlim=(-4.28, 4.28), ylim=(-0.92, 4.75), aspect="equal")
    ax.set_axis_off()

    ax.text(-4.10, 4.48, "Crossed Czerny–Turner spectrometer", color=TEXT,
            fontsize=19, weight="bold", va="center")
    ax.text(-4.10, 4.13, "Illustrative first-order ray paths in the dispersion plane",
            color=MUTED, fontsize=10.5, va="center")

    for index, path in enumerate(inputs):
        draw_ray(ax, path, color=NEUTRAL, chief=index == 1)
    for wavelength, paths in spectra.items():
        for index, path in enumerate(paths):
            draw_ray(ax, path, color=WAVELENGTH_COLORS[wavelength], chief=index == 1)
        arrow(ax, *paths[1][:2], WAVELENGTH_COLORS[wavelength], 0.61)
    arrow(ax, *inputs[1][1:], NEUTRAL, 0.51)
    arrow(ax, *inputs[1][:2], NEUTRAL, 0.22)
    arrow(ax, *spectra[550][1][1:], WAVELENGTH_COLORS[550], 0.78)

    for optic, span in ((collimator, COLLIMATOR_HALF_WIDTH), (camera, CAMERA_HALF_WIDTH)):
        curve = np.array([optic.surface(offset) for offset in np.linspace(-span, span, 101)])
        ax.plot(*curve.T, color="#445366", linewidth=7, solid_capstyle="round", zorder=4)
        ax.plot(*curve.T, color=OPTIC, linewidth=2.5, solid_capstyle="round", zorder=5)

    def optic_line(center, direction, half_width, *, color=OPTIC, width=3):
        ax.plot(*np.vstack((center - half_width * direction, center + half_width * direction)).T,
                color=color, linewidth=width, solid_capstyle="round", zorder=5)

    optic_line(grating, tangent, GRATING_HALF_WIDTH, width=4)
    back = -perpendicular(tangent)
    for distance in np.linspace(-0.49, 0.49, 11):
        mark = grating + distance * tangent
        ax.plot(*np.vstack((mark, mark + 0.105 * back)).T, color="#758699", linewidth=1.2, zorder=4)

    slit_tangent = perpendicular(unit(inputs[1][1] - slit))
    for direction in (-1, 1):
        optic_line(slit + direction * 0.18 * slit_tangent, slit_tangent, 0.12)
    incoming_start = slit - 0.79 * unit(inputs[1][1] - slit)
    draw_ray(ax, np.array([incoming_start, slit]), color=NEUTRAL, chief=True)
    arrow(ax, incoming_start, slit, NEUTRAL, 0.56)

    detector_axis = perpendicular(detector_normal)
    detector_hits = [path[-1] for paths in spectra.values() for path in paths]
    detector_span = max(abs(np.dot(hit - detector, detector_axis)) for hit in detector_hits) + 0.13
    optic_line(detector, detector_axis, detector_span, width=5)
    for wavelength, paths in spectra.items():
        ax.scatter(*paths[1][-1], c=WAVELENGTH_COLORS[wavelength], s=21, edgecolors=PANEL, linewidths=0.5, zorder=6)

    def component(text, xy, xytext, alignment="center"):
        ax.annotate(
            text, xy=xy, xytext=xytext, ha=alignment, va="center", color=TEXT, fontsize=11.1,
            linespacing=1.4, arrowprops={"arrowstyle": "-", "color": "#8594a4", "lw": 0.9},
            zorder=7,
        )

    component("Collimating\nmirror", collimator.vertex, (-3.26, 3.60))
    component("Focusing\nmirror", camera.vertex, (3.34, 3.60))
    component("Entrance slit", slit, (2.64, 1.00), "left")
    component("Detector array", detector, (-2.72, 1.00), "right")
    component("Plane reflection grating", grating, (0, -0.57))
    for index, (wavelength, color) in enumerate(WAVELENGTH_COLORS.items()):
        x = 1.87 + 0.77 * index
        ax.plot([x, x + 0.14], [-0.59, -0.59], color=color, linewidth=2, solid_capstyle="round")
        ax.text(x + 0.20, -0.59, f"{wavelength} nm", color=MUTED, fontsize=8.2, va="center")

    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"Date": None} if destination.suffix == ".svg" else None
    figure.savefig(destination, dpi=170, facecolor=PANEL, metadata=metadata)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(__file__).resolve().parents[1] / "docs" / "media" / "czerny-turner.svg"
    parser.add_argument("--output", type=Path, default=default)
    render(parser.parse_args().output)
