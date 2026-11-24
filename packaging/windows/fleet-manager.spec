# PyInstaller spec for the AREDN Fleet Manager desktop app (one-folder build).
# Build:  pyinstaller packaging/windows/fleet-manager.spec --noconfirm
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

HERE = Path(SPECPATH)
ROOT = HERE.parent.parent

datas = collect_data_files("aredn_fleet")          # web/static/*
binaries = []
hiddenimports = (collect_submodules("aredn_fleet")
                 + collect_submodules("uvicorn")
                 + collect_submodules("keyring.backends"))
# webview's Windows backend runs on .NET via pythonnet (clr / clr_loader); bundle
# their managed DLLs too, or the window fails to open on a machine without them.
for pkg in ("webview", "asyncssh", "pythonnet", "clr_loader"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:  # not installed on this platform (e.g. pythonnet on Linux)
        continue
    datas += d
    binaries += b
    hiddenimports += h
if sys.platform == "win32":
    hiddenimports += ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]

a = Analysis(
    [str(HERE / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AREDN-Fleet-Manager",
    icon=str(HERE / "icon.ico"),
    console=False,        # windowed app; --selftest still returns an exit code
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AREDN-Fleet-Manager", upx=False)
