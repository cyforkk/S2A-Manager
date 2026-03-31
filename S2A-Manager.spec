# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import sys

PYTHON_ROOT = Path(os.environ.get("PYINSTALLER_PYTHON_ROOT") or sys.base_prefix)
if not (PYTHON_ROOT / "tcl").exists():
    PYTHON_ROOT = Path(sys.executable).resolve().parent

tk_datas = [
    (str(PYTHON_ROOT / "tcl" / "tcl8.6"), "_tcl_data"),
    (str(PYTHON_ROOT / "tcl" / "tk8.6"), "_tk_data"),
    (str(PYTHON_ROOT / "tcl" / "tcl8"), "tcl8"),
    ("VERSION", "."),
]

a = Analysis(
    ['tools\\s2a_manager.py'],
    pathex=[],
    binaries=[],
    datas=tk_datas,
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.filedialog',
        'tkinter.messagebox',
        '_tkinter',
        'chatgpt_register_adapter',
        'curl_cffi',
        'curl_cffi.requests',
        'requests',
    ],
    hookspath=['pyinstaller_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='S2A-Manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
