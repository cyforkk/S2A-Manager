# -*- mode: python ; coding: utf-8 -*-
"""跨平台 PyInstaller 配置（Windows / macOS / Linux）。"""

import os
import sys
from pathlib import Path

PYTHON_ROOT = Path(os.environ.get("PYINSTALLER_PYTHON_ROOT") or sys.base_prefix)
if not (PYTHON_ROOT / "tcl").exists():
    # 部分安装布局把 tcl 放在可执行文件旁
    candidate = Path(sys.executable).resolve().parent
    if (candidate / "tcl").exists():
        PYTHON_ROOT = candidate

ENTRY = str(Path("tools") / "s2a_manager.py")

tk_datas = [("VERSION", ".")]
for folder, dest in (
    ("tcl8.6", "_tcl_data"),
    ("tk8.6", "_tk_data"),
    ("tcl8", "tcl8"),
):
    src = PYTHON_ROOT / "tcl" / folder
    if src.is_dir():
        tk_datas.append((str(src), dest))

hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "_tkinter",
    "chatgpt_register_adapter",
    "chatgpt_register_gui",
    "curl_cffi",
    "curl_cffi.requests",
    "requests",
]

a = Analysis(
    [ENTRY],
    pathex=[],
    binaries=[],
    datas=tk_datas,
    hiddenimports=hiddenimports,
    hookspath=["pyinstaller_hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Windows 无控制台；macOS/Linux 同样窗口应用
# UPX 在部分 macOS/Linux 环境不稳定，默认关闭以保证 CI 成功率
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="S2A-Manager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
