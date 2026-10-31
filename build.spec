# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 SnapOCR.exe（含 OCR 模型）。

用法：
    python -m PyInstaller --noconfirm --clean build.spec
或双击 build.bat。产物：dist\\SnapOCR.exe。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# ppocrv6_onnx 自带模型（det.onnx / rec.onnx / chars.txt），必须一并收集；
# 运行时路径为 Path(__file__).parent / "data"，在 _MEIPASS 内天然命中。
datas, binaries, hiddenimports = collect_all("ppocrv6_onnx")
hiddenimports += ["pystray._win32"]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# PyInstaller 会在 PATH 中搜索 DLL，可能优先捡到陈旧 VC++ 运行库
# （本机为 Eclipse Adoptium JRE 11 的 14.16），导致 exe 启动即 0xc0000005。
# 一律丢弃这些来源，改用 System32 的当前版本；UCRT 转发 DLL 交给系统。
_BAD_PATH_MARKERS = ("\\adoptium\\", "\\jre-", "\\jdk-")
_binaries = []
for _dest, _src, _typ in a.binaries:
    _low = _src.lower()
    if any(_marker in _low for _marker in _BAD_PATH_MARKERS):
        continue
    _binaries.append((_dest, _src, _typ))
a.binaries = _binaries

_sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
_runtime_dlls = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
_present = {_dest.lower() for _dest, _src, _typ in a.binaries}
for _name in _runtime_dlls:
    _src = _sys32 / _name
    if _src.exists() and _name not in _present:
        a.binaries.append((_name, str(_src), "BINARY"))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SnapOCR",
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
    icon="assets/snapocr.ico",
)
