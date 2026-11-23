# PyInstaller spec for dictate -- onedir, windowed (no console).
# Build:  packaging\build.cmd   (or: python -m PyInstaller packaging\dictate.spec --noconfirm)
# Output: dist\dictate\dictate.exe plus dist\dictate\_internal\
# The speech model is NOT bundled; dictate downloads it on first start (dictate/models.py).
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "dictate_main.py")],
    pathex=[str(ROOT)],
    binaries=collect_dynamic_libs("sherpa_onnx"),
    datas=[
        (str(ROOT / "dictate" / "prompts"), "dictate/prompts"),  # cleanup.py reads them next to its own file
        (str(ROOT / "assets" / "dictate.ico"), "assets"),  # app.py: TRAY_ICON_PATH
    ],
    hiddenimports=[
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
        "pystray._win32",
    ],
    excludes=["pytest", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dictate",
    icon=str(ROOT / "assets" / "dictate.ico"),
    console=False,
    upx=False,  # UPX-packed exes trip antivirus far more often
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="dictate")
