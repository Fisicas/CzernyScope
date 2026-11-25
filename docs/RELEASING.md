# Building a Windows release

Use a separate 64-bit Python 3.12 environment without system site packages for packaging. Install Inno Setup 6.3 or later from the 6.x series to build the per-user Windows installer. The Python runtime and USB libraries are included in the packaged app. A physical spectrometer still needs a WinUSB-compatible driver. [Inno Setup downloads](https://jrsoftware.org/inno-setup/downloads)

## Build locally

From the project root, create a build environment and install the packaging tools.

```powershell
py -3.12 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install ".[hardware]" -r requirements-build.txt pytest==9.1.1
.\.venv-build\Scripts\python.exe -X utf8 -m pytest -q
.\.venv-build\Scripts\python.exe -X utf8 tools/build_release.py --output dist/local-v0.4.0 --allow-local
```

Choose a new output directory for each build. Use `--iscc` with the full path to `ISCC.exe` if the compiler is outside the standard locations. Use `--portable-only` to build the ZIP when the compiler is unavailable.

The `packages` subfolder contains the portable ZIP, installer, SHA-256 checksum files for the binaries produced, and build manifest. The build produces unsigned binaries. To check the portable build, extract it into a separate directory, open `CzernyScope.exe`, and run **Instrument → Open simulated instrument → Maya2000Pro**. Test a fresh install and an in-place upgrade in a disposable Windows account or VM before distributing the installer. Verify that saved spectra and dark references remain accessible after the upgrade.

## Prepare stable releases

Keep the versions in `pyproject.toml` and `spectrometer_app/__init__.py` in sync. A stable build uses a clean checkout at the matching `vX.Y.Z` tag.

```powershell
.\.venv-build\Scripts\python.exe -X utf8 tools/build_release.py --output dist/v0.4.0 --tag v0.4.0
```

The Windows build workflow runs either on request or for a version tag. It tests the app and uploads build artifacts for review. To make an update visible to installed copies, publish a stable GitHub Release for the matching tag with the reviewed installer, portable ZIP, checksum files, and manifest. A source push or tag alone does not make an update available. [GitHub release API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)

On an older build, verify that **Help → Check for updates** finds the published release. Close CzernyScope before running the installer. The updater opens the release page for the user to download the installer.

## Binary distribution review

Check the paths in PyInstaller’s `work/czernyscope/Analysis-00.toc` and the contents of the actual ZIP. Verify that the app carries the intended Qt and USB runtime files and a single native libusb DLL from the pinned `libusb-package` distribution. The build manifest records dependency versions and archive hashes. Repeat the fresh-install and in-place-upgrade checks using the tagged payload, including a check that an existing spectrum and dark library remain readable.

Before distributing the installer or ZIP, identify all bundled native dependencies and complete their third-party notices and corresponding-source arrangements. For LGPL distribution of Qt/PySide and the native libusb DLL, review the licenses, access to matching source, and the recipient’s ability to use a replacement library. The native libusb library has its own license alongside the Python wrapper. Verify the Python runtime license in the actual bundle. [Qt distribution guidance](https://www.qt.io/development/open-source-lgpl-obligations) · [Native libusb license](https://github.com/libusb/libusb/blob/v1.0.30/COPYING) · [CPython license](https://docs.python.org/3.12/license.html)

The development folder also contains local reference materials. Keep the vendor SDK sample pack, local notes, and any lab data outside the files you stage for publication.
