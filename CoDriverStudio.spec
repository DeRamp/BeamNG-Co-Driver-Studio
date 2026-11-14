# PyInstaller spec — one-folder build, windowed, no console.
# Build: .venv\Scripts\python -m PyInstaller --noconfirm CoDriverStudio.spec
import os
from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None
root = os.path.abspath(SPECPATH)

a = Analysis(
    [os.path.join(root, "run_app.py")],
    pathex=[root],
    binaries=collect_dynamic_libs("soundfile") + collect_dynamic_libs("sounddevice"),
    datas=[(os.path.join(root, "codriver_studio", "data"), os.path.join("codriver_studio", "data"))],
    hiddenimports=["_soundfile_data", "_sounddevice_data"],
    excludes=["tkinter", "scipy", "matplotlib", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CoDriverStudio",
    console=False,
    icon=os.path.join(root, "codriver_studio", "data", "icon.ico") if os.path.exists(os.path.join(root, "codriver_studio", "data", "icon.ico")) else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="CoDriverStudio", upx=False)
