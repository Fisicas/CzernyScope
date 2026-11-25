"""Share the source app entry point and provide an opt-in frozen-build smoke check."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from spectrometer_app.__main__ import main


def _smoke(report: Path) -> int:
    from PySide6.QtCore import QTimer
    import libusb_package
    import seabreeze
    import usb

    from spectrometer_app import APP_NAME, __version__, window
    from spectrometer_app.hardware import _usb_backend

    class SmokeWindow(window.SpectrumWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.smoke_timer = QTimer(self)
            self.smoke_timer.setInterval(40)
            self.smoke_timer.timeout.connect(self._record_frame)
            self.smoke_timer.start()
            QTimer.singleShot(15_000, self, self._timeout)

        def _timeout(self):
            if self.smoke_timer.isActive():
                self.smoke_timer.stop()
                report.write_text(json.dumps({"error": "No live demo frame arrived."}), encoding="utf-8")
                self.close()

        def _record_frame(self):
            frame = self.plot.latest
            if frame is None:
                return
            self.smoke_timer.stop()
            try:
                stats = self._checks(frame)
            except Exception as error:
                stats = {"error": str(error)}
            report.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
            self.close()

        def _checks(self, frame):
            bundle = Path(sys._MEIPASS)
            native = libusb_package.get_library_path()
            hardware_paths = [Path(module.__file__) for module in (libusb_package, seabreeze, usb)]
            bundled_hardware = native is not None and all(
                path.is_relative_to(bundle) for path in [native, *hardware_paths]
            )
            backend, *_ = _usb_backend()
            return {
                "name": APP_NAME,
                "version": __version__,
                "title": self.windowTitle(),
                "visible": self.isVisible(),
                "icon": not self.windowIcon().isNull(),
                "pixels": frame.wavelengths_nm.size,
                "synthetic": frame.info.backend == "demo",
                "bundled_hardware": bundled_hardware and backend is not None,
            }

    window.SpectrumWindow = SmokeWindow
    sys.argv[1:] = ["--demo", "Maya2000Pro", "--data-dir", str(report.parent / "smoke-data")]
    return main()


if __name__ == "__main__":
    report_path = os.environ.get("CZERNYSCOPE_BUILD_SMOKE")
    raise SystemExit(_smoke(Path(report_path)) if report_path else main())
