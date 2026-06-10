# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — desktop build (onedir; ship the zipped dist/InvoiceProcessor folder).
#
#   pyinstaller desktop/InvoiceProcessor.spec --noconfirm
#
# pyzbar is deliberately excluded: it needs the system zbar shared library,
# which PyInstaller does not collect. qr_swiss.py treats it as optional and
# zxing-cpp (binary bundled in its wheel) handles QR decoding in this build.
from pathlib import Path

ROOT = Path(SPECPATH).parent
APP = ROOT / "app"

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(APP)],  # flat app modules (main, db, paths, …) import top-level
    binaries=[],
    datas=[
        (str(APP / "templates"), "templates"),
        (str(APP / "static"), "static"),
        (str(APP / "schemas"), "schemas"),
        (str(Path(SPECPATH) / "settings.env.template"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pyzbar", "tests", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="InvoiceProcessor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # console window doubles as the "close to quit" affordance
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="InvoiceProcessor",
)
