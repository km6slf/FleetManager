# PyInstaller spec for the AREDN Fleet Manager desktop app (one-folder build).
# Build:  pyinstaller packaging/windows/fleet-manager.spec --noconfirm
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

HERE = Path(SPECPATH)
ROOT = HERE.parent.parent

datas = collect_data_files("aredn_fleet")          # web/static/*
binaries = []
hiddenimports = (collect_submodules("aredn_fleet")
                 + collect_submodules("uvicorn")
                 + collect_submodules("keyring.backends"))
for pkg in ("webview", "asyncssh"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

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
