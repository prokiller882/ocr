"""main —— SnapOCR 入口：装配 App + Tk 主循环。

开发态经 run.py 调用；打包态由 PyInstaller 以 run.py 为入口（见 build.spec）。
主线程持有隐藏的 Tk root；各窗口为弹出式 Toplevel，应用常驻托盘（Spec §3）。
"""
from __future__ import annotations

import tkinter as tk

from .config import load
from .cores.logger import get_logger
from .cores.paths import ensure_app_dir
from .cores.win32 import enable_dpi_awareness
from .modules.app import App
from .modules.history import HistoryStore
from .modules.ocr_engine import OCRRecognizer


def main() -> None:
    enable_dpi_awareness()  # 必须在 Tk root 创建前调用，保证截图坐标一致
    logger = get_logger()
    logger.info("SnapOCR 启动")

    app_dir = ensure_app_dir()
    config = load()

    root = tk.Tk()
    root.withdraw()  # 无固定主窗口，隐藏 root（弹窗为独立 Toplevel，不依赖 root 可见性）

    store = HistoryStore(app_dir, limit=config.history_limit)
    recognizer = OCRRecognizer(
        det_model_path=config.ocr_det_model,
        rec_model_path=config.ocr_rec_model,
        rec_char_dict_path=config.ocr_char_dict,
        det_thresh=config.ocr_det_thresh,
        det_box_thresh=config.ocr_det_box_thresh,
        det_unclip_ratio=config.ocr_det_unclip_ratio,
        rec_batch_size=config.ocr_rec_batch_size,
        preprocess_scale=config.preprocess_scale,
        preprocess_padding=config.preprocess_padding,
    )

    app = App(root, config, store, recognizer)
    # 防 GC：App/EventBus 通过 root 的 after 回调持有，显式挂到 root 保险
    root._snap_ocr_app = app  # type: ignore[attr-defined]

    root.mainloop()
    logger.info("SnapOCR 已退出")
