"""Prepare a frozen Windows app that uses Microsoft's system VC runtime."""

import hashlib
from pathlib import Path
import re
import subprocess
import sys

import pefile


NUMPY_RUNTIME_ALIAS = "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
NUMPY_RUNTIME_IMAGES = {
    "numpy/_core/_multiarray_umath.cp312-win_amd64.pyd",
    "numpy/fft/_pocketfft_umath.cp312-win_amd64.pyd",
}
RUNTIME_DLL = re.compile(r"(?:concrt140|msvcp140|vccorlib140|vcruntime140)[-_a-z0-9]*\.dll")
IMPORT_DIRECTORIES = [
    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
]


def _imports(path: Path) -> list[str]:
    with pefile.PE(str(path), fast_load=True) as image:
        image.parse_data_directories(directories=IMPORT_DIRECTORIES)
        return [
            entry.dll.decode("ascii").lower()
            for attribute in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT")
            for entry in getattr(image, attribute, ())
        ]


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_system_runtime(app_bundle: Path, env: dict) -> dict:
    internal = app_bundle / "_internal"
    images = {
        path.relative_to(internal).as_posix(): (path, _imports(path))
        for path in internal.rglob("*")
        if path.suffix.lower() in {".dll", ".pyd", ".exe"} and path.is_file()
    }
    aliased = {name for name, (_, imports) in images.items() if NUMPY_RUNTIME_ALIAS in imports}
    if aliased != NUMPY_RUNTIME_IMAGES:
        raise ValueError(f"Review the pinned NumPy wheel's VC runtime imports. Found {sorted(aliased)}.")

    changed = {}
    for name in sorted(NUMPY_RUNTIME_IMAGES):
        path, before_imports = images[name]
        before = _sha256(path)
        subprocess.run(
            [sys.executable, "-m", "delvewheel", "replace-needed", str(path),
             "-change", NUMPY_RUNTIME_ALIAS, "msvcp140.dll"],
            check=True, env=env, timeout=60,
        )
        expected = ["msvcp140.dll" if dll == NUMPY_RUNTIME_ALIAS else dll for dll in before_imports]
        if _imports(path) != expected:
            raise ValueError(f"Unexpected changes to the Windows imports in {name}.")
        changed[name] = {"original_sha256": before, "sha256": _sha256(path)}

    excluded = {}
    for name, (path, _) in images.items():
        if RUNTIME_DLL.fullmatch(path.name.lower()):
            excluded[name] = _sha256(path)
            path.unlink()
    if f"numpy.libs/{NUMPY_RUNTIME_ALIAS}" not in excluded:
        raise ValueError("The expected private NumPy VC runtime DLL was missing from the freeze.")
    return {"source": "Microsoft Visual C++ v14 x64 system runtime",
            "modified_numpy_extensions": changed, "excluded_dlls": excluded}
