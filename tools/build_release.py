"""Build and verify a local CzernyScope Windows release bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
APP_NAME = "CzernyScope"


def source_version() -> str:
    source = (ROOT / "spectrometer_app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', source, re.MULTILINE)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if not match or project["project"]["version"] != match[1]:
        raise ValueError("Set matching stable versions in pyproject.toml and spectrometer_app/__init__.py.")
    return match[1]


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *args], text=True, encoding="utf-8", stderr=subprocess.PIPE,
    ).strip()


def source_identity(version: str, tag: str | None) -> dict:
    try:
        commit = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain", "--untracked-files=all"))
    except (OSError, subprocess.CalledProcessError):
        if tag is not None:
            raise ValueError("A tagged release requires a working Git checkout.") from None
        commit, dirty = None, None
    if tag is not None:
        if tag != f"v{version}" or dirty:
            raise ValueError(f"Use a clean checkout and the release tag v{version}.")
        try:
            tagged_commit = _git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            raise ValueError(f"Required local release tag {tag} was not found.") from None
        if tagged_commit != commit:
            raise ValueError("The release tag must point to HEAD.")
    return {"version": version, "commit": commit, "dirty": dirty, "tag": tag, "local_candidate": tag is None}


def find_inno(explicit: Path | None) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise ValueError(f"Inno Setup compiler was not found at {explicit}.")
    on_path = shutil.which("ISCC.exe")
    if on_path is not None:
        return Path(on_path).resolve()
    for env_name, suffix in (
        ("LOCALAPPDATA", "Programs/Inno Setup 6/ISCC.exe"),
        ("ProgramFiles(x86)", "Inno Setup 6/ISCC.exe"),
        ("ProgramFiles", "Inno Setup 6/ISCC.exe"),
    ):
        base = os.environ.get(env_name)
        if base and (candidate := Path(base) / suffix).is_file():
            return candidate
    profile_compiler = Path.home() / "AppData/Local/Programs/Inno Setup 6/ISCC.exe"
    if profile_compiler.is_file():
        return profile_compiler
    raise ValueError("Install Inno Setup 6.3+, pass --iscc, or build with --portable-only.")


def require_tools() -> None:
    if sys.version_info[:3] != (3, 12, 10) or ssl.OPENSSL_VERSION.split()[:2] != ["OpenSSL", "3.0.16"]:
        raise ValueError("Use the CPython 3.12.10 Windows build with OpenSSL 3.0.16 referenced in the notices.")
    missing = [
        name for name in ("PyInstaller", "pefile", "delvewheel", "numpy", "matplotlib", "PySide6",
                          "seabreeze", "usb", "libusb_package")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise ValueError(
            f"Build environment is missing {', '.join(missing)}. "
            'Use a separate environment and install ".[hardware]" and -r requirements-build.txt.'
        )


def _build_environment(output: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    windows = Path(os.environ.get("SystemRoot", ""))
    if not windows.is_absolute() or not (windows / "System32").is_dir():
        raise ValueError("Windows did not provide a valid SystemRoot for the build environment.")
    python = Path(sys.base_prefix)
    search_dirs = (Path(sys.executable).parent, python, python / "DLLs", windows / "System32", windows)
    # PyInstaller resolves native dependencies through PATH. Use only the
    # selected Python and Windows directories so other tools cannot supply DLLs.
    env["PATH"] = os.pathsep.join(str(directory) for directory in dict.fromkeys(search_dirs))
    env["PYINSTALLER_CONFIG_DIR"] = str(output / "pyinstaller-cache")
    env["MPLCONFIGDIR"] = str(output / "matplotlib-cache")
    return env


def _run(*command, cwd: Path, timeout: int, env=None) -> None:
    argv = [str(argument) for argument in command]
    print(subprocess.list2cmdline(argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True, timeout=timeout, env=env)


def _verify_windows_product(path: Path, version: str, *, x64: bool = False) -> None:
    import pefile

    with pefile.PE(str(path), fast_load=False) as pe:
        if x64 and pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError(f"{path.name} is not a Windows x64 executable.")
        product = pe.VS_FIXEDFILEINFO[0]
        actual = ".".join(map(str, (
            product.ProductVersionMS >> 16, product.ProductVersionMS & 0xffff,
            product.ProductVersionLS >> 16,
        )))
        if actual != version:
            raise ValueError(f"{path.name} reports version {actual} instead of {version}.")


def _smoke(executable: Path, output: Path, version: str, env: dict) -> dict:
    report = output / "demo_smoke.json"
    smoke_env = {**env, "QT_QPA_PLATFORM": "offscreen", "CZERNYSCOPE_BUILD_SMOKE": str(report)}
    _run(executable, cwd=output, env=smoke_env, timeout=90)
    results = json.loads(report.read_text(encoding="utf-8"))
    expected = {
        "name": APP_NAME, "version": version, "title": APP_NAME, "visible": True,
        "icon": True, "pixels": 2048, "synthetic": True, "bundled_hardware": True,
    }
    if results != expected:
        raise ValueError(f"Frozen demo or bundled USB runtime did not pass verification. See {report}.")
    return results


def _write_checksums(artifacts: list[Path]) -> dict[str, str]:
    hashes = {}
    for path in artifacts:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        hashes[path.name] = digest
        path.with_suffix(path.suffix + ".sha256").write_text(
            f"{digest}  {path.name}\n", encoding="ascii"
        )
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New build output directory")
    identity_flag = parser.add_mutually_exclusive_group(required=True)
    identity_flag.add_argument("--tag", help="Build a release from the clean checkout at vX.Y.Z")
    identity_flag.add_argument("--allow-local", action="store_true", help="Build an unpublished local candidate")
    parser.add_argument("--iscc", type=Path, help="Path to the Inno Setup compiler")
    parser.add_argument("--portable-only", action="store_true", help="Build a portable ZIP without an installer")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Use 64-bit Python on Windows.")
    if args.portable_only and args.iscc is not None:
        parser.error("Choose --portable-only or --iscc.")
    try:
        version = source_version()
        identity = source_identity(version, args.tag)
        output = args.output.resolve()
        if ROOT.is_relative_to(output) or output.exists():
            raise ValueError("Choose an output directory that is new and does not contain the source checkout.")
        compiler = None if args.portable_only else find_inno(args.iscc)
        require_tools()
        env = _build_environment(output)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    from windows_runtime import prepare_system_runtime

    output.mkdir(parents=True, exist_ok=False)
    packages = output / "packages"
    packages.mkdir()
    app_directory = output / "app"
    _run(
        sys.executable, "-X", "utf8", "-m", "PyInstaller", "--noconfirm",
        "--distpath", app_directory, "--workpath", output / "work", PACKAGING / "czernyscope.spec",
        cwd=ROOT, timeout=1200, env=env,
    )
    app_bundle = app_directory / APP_NAME
    system_runtime = prepare_system_runtime(app_bundle, env)
    prerequisite = (PACKAGING / "msvc_prerequisite.iss").read_bytes()
    system_runtime["prerequisite_script_sha256"] = hashlib.sha256(prerequisite).hexdigest()
    executable = app_bundle / f"{APP_NAME}.exe"
    _verify_windows_product(executable, version, x64=True)
    demo = _smoke(executable, output, version, env)
    portable = Path(shutil.make_archive(
        str(packages / f"{APP_NAME}-{version}-win64"), "zip", root_dir=app_directory, base_dir=APP_NAME,
    ))
    with zipfile.ZipFile(portable) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValueError(f"Could not verify ZIP member {corrupt}.")
    artifacts = [portable]
    if compiler is not None:
        _run(
            compiler, f"/DBundleDir={app_bundle}", f"/O{packages}", PACKAGING / "czernyscope.iss",
            cwd=ROOT, timeout=600, env=env,
        )
        installer = packages / f"{APP_NAME}-{version}-setup.exe"
        _verify_windows_product(installer, version)
        artifacts.append(installer)
    manifest = {
        **identity,
        "python": sys.version,
        "dependencies": {
            package.metadata["Name"]: package.version for package in importlib.metadata.distributions()
        },
        "assets_sha256": _write_checksums(artifacts),
        "windows_runtime": system_runtime,
        "frozen_demo": demo,
        "portable_zip_verified": True,
        "installer_compiled": compiler is not None,
        "installer_executed": False,
        "signed": False,
    }
    (packages / "build_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Verified local build in {packages}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
