"""Run CzernyScope from a checkout or an installed package."""

import argparse
import sys
import warnings
from pathlib import Path

from . import APP_NAME, __version__


def _default_data_dir():
    documents = Path.home() / "Documents"
    previous = documents / "Spectrometer Live"
    return previous if previous.is_dir() else documents / APP_NAME


def _set_windows_app_id():
    if sys.platform != "win32":
        return

    from ctypes import WinDLL, c_long, c_wchar_p

    set_app_id = WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
    set_app_id.argtypes = [c_wchar_p]
    set_app_id.restype = c_long
    result = set_app_id("SpectrometerLive.Desktop")
    if result < 0:
        warnings.warn(f"Windows taskbar identity could not be set (0x{result & 0xFFFFFFFF:08X}).")


def main():
    parser = argparse.ArgumentParser(prog="czernyscope", description="Live acquisition for Maya2000 Pro and USB650.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--demo", choices=("Maya2000Pro", "USB650"), help="Open a simulated instrument.")
    parser.add_argument(
        "--data-dir", type=Path, default=_default_data_dir(),
        help="Base directory for saved spectra and dark libraries.",
    )
    parser.add_argument("--probe", action="store_true", help="List supported USB devices without opening them.")
    options = parser.parse_args()
    if options.probe:
        from .hardware import list_devices

        try:
            devices = list_devices()
        except RuntimeError as exc:
            parser.exit(1, f"{exc}\n")
        for device in devices:
            print(device.label)
        if not devices:
            print("No supported USB spectrometer detected.")
        return 0

    _set_windows_app_id()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .window import SpectrumWindow

    app = QApplication([])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "spectrometer-live.ico")))
    window = SpectrumWindow(options.data_dir.expanduser().resolve(), options.demo)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
