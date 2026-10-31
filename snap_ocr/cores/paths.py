"""paths —— 运行时目录定位。

解析规则（DESIGN §1 / §3）：
    - 开发态：run.py 所在目录（本项目根），log/json 与启动文件同目录
    - 打包态（PyInstaller --onefile）：exe 同目录
    - 可选环境变量 SNAP_OCR_DATA_DIR 优先覆盖（🔄 可演进）

布局（用户要求：启动目录下仅三个文件，无任何子文件夹）：
    <启动目录>/config.json     配置
    <启动目录>/history.json    全部历史记录（含截图 base64，单文件自包含）
    <启动目录>/app.log         日志（滚动）

模型路径走 ppocrv6_onnx 包内 data/（wheel 自带）；打包态由
--collect-all ppocrv6_onnx 解压到 _MEIPASS，`Path(__file__).parent / data`
在包内天然命中，故模型解析无需在此特殊处理。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境。"""
    return bool(getattr(sys, "frozen", False))


def get_app_dir() -> Path:
    """返回运行时目录（log/json 存放处）= 启动文件所在目录，无 data 层。

    开发态为项目根（run.py 所在目录）；打包态为 exe 同目录。
    """
    env = os.environ.get("SNAP_OCR_DATA_DIR")
    if env:
        return Path(env)
    if is_frozen():
        # 打包态：exe 同目录
        return Path(sys.executable).resolve().parent
    # 开发态：项目根（run.py 所在目录）
    return Path(__file__).resolve().parents[2]


def ensure_app_dir() -> Path:
    """确保运行时目录存在并返回其路径（log/json 直接放该目录）。"""
    app_dir = get_app_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir
