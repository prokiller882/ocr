"""SnapOCR 开发态入口。

使用方式：
    1. 已存在可用的 Python 解释器并安装了依赖：直接 `python run.py`
    2. 推荐：双击 run.bat —— 自动激活 conda 环境 train_env 后运行
       （run.bat 不再自建 .venv，依赖全部来自 train_env）

本文件只做「依赖就绪检查 + 启动」，实际装配见 snap_ocr.main.main()。
"""
from __future__ import annotations

import sys


def check_dependencies() -> None:
    """校验核心运行依赖是否可导入；缺失时给出清晰提示。"""
    mods = [
        "ppocrv6_onnx",  # OCR 引擎
        "onnxruntime",   # 引擎传递依赖
        "cv2",           # 引擎传递依赖
        "numpy",         # 引擎传递依赖
        "PIL",           # 截图 / 图标
        "pystray",       # 托盘
        "tkinter",       # 界面（标准库）
    ]
    missing = []
    for m in mods:
        try:
            __import__(m)
        except Exception as exc:  # ImportError / ModuleNotFoundError
            missing.append((m, exc))
    if missing:
        print("====== SnapOCR: отсутствуют зависимости ======")
        for name, exc in missing:
            print(f"  - {name}: {exc}")
        print("\nУстановите:  pip install -r requirements.txt   (или запустите run.bat)")
        sys.exit(1)


def main() -> None:
    check_dependencies()
    # 延迟导入，避免缺依赖时连模块都解析失败
    from snap_ocr.main import main as app_main

    app_main()


if __name__ == "__main__":
    main()
