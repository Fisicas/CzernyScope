# -*- mode: python ; coding: utf-8 -*-
"""Freeze CzernyScope as a Windows x64 one-folder application."""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)


ROOT = Path(SPECPATH).parent
ICON = ROOT / "spectrometer_app" / "assets" / "spectrometer-live.ico"
CPYTHON_LICENSE = Path(sys.base_prefix) / "LICENSE.txt"
if not CPYTHON_LICENSE.is_file():
    raise ValueError(f"CPython's LICENSE.txt is missing from {sys.base_prefix}.")
source = (ROOT / "spectrometer_app" / "__init__.py").read_text(encoding="utf-8")
version = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', source, re.MULTILINE)[1]
version_tuple = tuple(map(int, version.split("."))) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("ProductName", "CzernyScope"),
            StringStruct("FileDescription", "CzernyScope live spectrum acquisition"),
            StringStruct("ProductVersion", version),
            StringStruct("FileVersion", version),
            StringStruct("OriginalFilename", "CzernyScope.exe"),
            StringStruct("InternalName", "CzernyScope"),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

datas = [
    (str(ICON), "spectrometer_app/assets"),
    (str(Path(SPECPATH) / "LIGHTKEEPER-LICENSE.txt"), "."),
    (str(CPYTHON_LICENSE), "."),
    (str(Path(SPECPATH) / "third-party/THIRD-PARTY-NOTICES.txt"), "."),
    (str(Path(SPECPATH) / "third-party/licenses"), "licenses"),
]
for distribution in (
    "seabreeze", "pyusb", "libusb-package", "importlib-resources", "numpy", "matplotlib",
    "contourpy", "cycler", "fonttools", "kiwisolver", "packaging", "pillow", "pyparsing",
    "python-dateutil", "six", "PySide6-Essentials", "shiboken6", "PyInstaller",
):
    datas += copy_metadata(distribution)

usb_destination = Path("libusb_package/libusb-1.0.dll")
usb_binary = [
    (source, folder) for source, folder in collect_dynamic_libs("libusb_package")
    if Path(folder) / Path(source).name == usb_destination
]
if len(usb_binary) != 1:
    raise ValueError("The selected libusb-package must provide its Windows libusb-1.0.dll.")
usb_source = Path(usb_binary[0][0]).resolve()

a = Analysis(
    [str(Path(SPECPATH) / "czernyscope_launcher.py")],
    pathex=[str(ROOT)],
    binaries=usb_binary,
    datas=datas,
    hiddenimports=[
        "usb.backend.libusb1", "matplotlib.backends.backend_qtagg",
        *collect_submodules("seabreeze.pyseabreeze"),
    ],
    hooksconfig={"matplotlib": {"backends": ["QtAgg"]}},
    module_collection_mode={"PySide6": "py", "shiboken6": "py"},
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "tkinter", "IPython", "seabreeze.cseabreeze",
        "pytest", "pandas", "scipy", "jinja2",
    ],
)


def keep_binary(entry):
    destination, source, _ = entry
    name = Path(destination).name.casefold()
    # The English Qt Widgets app uses the native Windows platform plugin.
    # Qt's optional software OpenGL fallback is unnecessary for QtAgg.
    if Path(destination) == Path("PySide6/opengl32sw.dll"):
        return False
    # Qt 6.11 uses Windows ICU. A private ICU can shadow it.
    if name == "icuuc.dll":
        return False
    # PyUSB's hook can find a system library and libusb-package's hook also
    # adds a root copy. CzernyScope loads the package-relative DLL explicitly.
    if name.endswith(".dll") and (
        name.startswith(("libusb", "cygusb", "openusb")) or name in {"usb.dll", "usb-0.1.dll", "usb-1.0.dll"}
    ):
        return Path(destination) == usb_destination and Path(source).resolve() == usb_source
    return True


a.binaries = [entry for entry in a.binaries if keep_binary(entry)]
a.datas = [
    entry for entry in a.datas
    if not (
        Path(entry[0]).is_relative_to(Path("PySide6/translations"))
        and Path(entry[0]).suffix == ".qm"
    )
]
if sum(Path(entry[0]) == usb_destination for entry in a.binaries) != 1:
    raise ValueError("The frozen app needs exactly one package-relative libusb-1.0.dll.")
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="CzernyScope", console=False,
    icon=str(ICON), version=version_info, upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CzernyScope", upx=False)
