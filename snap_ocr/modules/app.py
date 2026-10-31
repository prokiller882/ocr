"""app —— SnapOCR 应用装配与后台服务。

对应 DESIGN §2.1 / §1 线程模型：
    - 主线程：Tk mainloop（窗口/遮罩），EventBus 在此轮询派发
    - 托盘线程：pystray.run_detached，菜单回调经 EventBus 转主线程
    - 热键线程：WindowsHotkey 消息循环，WM_HOTKEY 经 EventBus 转主线程
    - OCR 工作线程：ThreadPoolExecutor，完成后结果经 EventBus 回主线程
    - 托盘"退出"是唯一真正结束进程的路径；其余窗口关闭只隐藏，应用驻留托盘
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageFont

from .. import config as config_mod
from ..config import Config
from ..cores.hotkey import WindowsHotkey, parse_hotkey
from ..cores.logger import get_logger
from .capture import (CaptureOverlay, CaptureResult, capture_background,
                      extract_region)
from .history import HistoryStore
from .ocr_engine import OCRRecognizer
from .ui import HistoryWindow, ResultWindow, SettingsWindow, show_notice

logger = get_logger(__name__)

_POLL_MS = 100          # EventBus 轮询间隔
_OCR_WORKERS = 1        # 单机单用户，1 个工作线程足够
_TRAY_TITLE = "SnapOCR — офлайн-распознавание со снимка экрана"


class EventBus:
    """跨线程 Tk 安全通道：queue.Queue + root.after 轮询（Spec §3 交互公理）。

    所有 Tk 操作必须经 post() 提交到主线程执行，禁止子线程直接操作 Tk。
    """

    def __init__(self, root: tk.Tk) -> None:
        self._q: queue.Queue = queue.Queue()
        self._root = root

    def start(self) -> None:
        self._root.after(_POLL_MS, self._poll)

    def post(self, fn, *args, **kwargs) -> None:
        self._q.put((fn, args, kwargs))

    def _poll(self) -> None:
        try:
            while True:
                fn, args, kwargs = self._q.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:  # noqa: BLE001 - 单次回调失败不影响后续
                    logger.exception("EventBus 回调异常: %s", getattr(fn, "__name__", fn))
        except queue.Empty:
            pass
        self._root.after(_POLL_MS, self._poll)


class SnapTrayIcon(pystray.Icon):
    """托盘图标：**双击**才触发截图（win32 WM_LBUTTONDBLCLK），单击不动作。

    pystray 的 win32 后端在 WM_LBUTTONUP（单击）就调用 default 菜单项，
    导致「单击即截图」的误触。这里改为拦截 WM_LBUTTONDBLCLK（双击）触发回调；
    单击（WM_LBUTTONUP）因不再有 default 项而变成无操作。
    """

    _WM_LBUTTONDBLCLK = 0x0203

    def __init__(self, *args, on_double_click=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_double_click = on_double_click

    def _on_notify(self, wparam, lparam) -> int:
        if lparam == self._WM_LBUTTONDBLCLK:
            cb = self._on_double_click
            if cb:
                try:
                    cb()
                except Exception:  # noqa: BLE001 - 回调异常不进入消息循环
                    logger.exception("托盘双击回调异常")
            return 0
        return super()._on_notify(wparam, lparam)


class App:
    """托盘 + 热键 + 事件路由 + 窗口装配 + 识别编排。"""

    def __init__(self, root: tk.Tk, config: Config, store: HistoryStore,
                 recognizer: OCRRecognizer) -> None:
        self._root = root
        self._config = config
        self._store = store
        self._recognizer = recognizer

        self._bus = EventBus(root)
        self._bus.start()

        self._executor = ThreadPoolExecutor(max_workers=_OCR_WORKERS,
                                            thread_name_prefix="snap-ocr")
        self._tray = None
        self._hotkey: WindowsHotkey | None = None

        self._setup_tray()
        self._register_hotkey()
        # 后台预热引擎（首帧提速），不阻塞启动
        self._executor.submit(self._recognizer.warmup)

    # =====================================================================
    # 托盘
    # =====================================================================

    def _setup_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Снимок экрана", lambda _i, _it: self._bus.post(self.start_capture)),
            pystray.MenuItem("История", lambda _i, _it: self._bus.post(self.show_history)),
            pystray.MenuItem("Настройки", lambda _i, _it: self._bus.post(self.show_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda _i, _it: self._bus.post(self.shutdown)),
        )
        # 不再用 default=True（pystray 单击即触发 default 项）；改为双击才截图
        self._tray = SnapTrayIcon(
            "SnapOCR", _make_tray_image(), _TRAY_TITLE, menu,
            on_double_click=lambda: self._bus.post(self.start_capture),
        )
        self._tray.run_detached()

    # =====================================================================
    # 全局热键
    # =====================================================================

    def _register_hotkey(self) -> None:
        spec = parse_hotkey(self._config.hotkey)
        if spec is None:
            logger.error("配置热键非法: %r，未注册", self._config.hotkey)
            return
        hk = WindowsHotkey(spec, lambda: self._bus.post(self.start_capture))
        if hk.register():
            self._hotkey = hk
            logger.info("全局热键已注册: %s", spec.display)
        else:
            self._hotkey = None
            logger.error("全局热键注册失败（可能被占用）: %s", spec.display)

    def _unregister_hotkey(self) -> None:
        if self._hotkey is not None:
            self._hotkey.unregister()
            self._hotkey = None

    # =====================================================================
    # 识别编排（Page_CaptureOverlay → 截图 → 立即弹结果窗 → OCR 刷新）
    # =====================================================================
    # 线程模型（Spec §3 / DESIGN §1）：截图 / OCR 识别 / 历史 I/O 均在子线程
    # (executor) 执行；主线程只做 Tk 操作（弹窗、刷新文本、剪贴板），
    # 保证托盘与主线程响应，不被耗时操作阻塞。

    def start_capture(self) -> None:
        """热键/托盘触发：**先截全屏背景** → 遮罩框选 → 确认后裁选区 → 识别。

        先截图再框选（微信/QQ 截图做法）：背景捕捉的是热键按下那一刻的屏幕，
        遮罩本身不会被截进去，也不依赖各窗口对 Tk 遮罩的透明叠加。
        """
        try:
            bg = capture_background()
        except Exception:  # noqa: BLE001 - 截图失败：仍允许框选，识别走失败提示
            logger.exception("全屏背景截图失败")
            bg = None
        overlay = CaptureOverlay(self._root,
                                 opacity=self._config.overlay_opacity,
                                 background=bg,
                                 style=self._config.overlay_style)
        result = overlay.select()
        if result is None:
            return  # 取消（Esc/右键），无副作用（Spec §4.1 Act_CancelSelect）
        # 子线程裁选区 + 识别：不阻塞主线程/托盘（旋转选区时返回「摆正」内容）
        fut = self._executor.submit(_safe_grab, bg, result)
        fut.add_done_callback(lambda f: self._bus.post(self._on_grabbed, f))

    def _on_grabbed(self, fut) -> None:
        """主线程：截图就绪 → 立即弹结果窗（图片 + 识别中…）→ 提交识别。"""
        image = fut.result()
        if isinstance(image, Exception):
            logger.exception("截图失败")
            show_notice(self._root, "Ошибка снимка",
                        f"Не удалось захватить экран: {image}",
                        auto_close_ms=3000)
            return
        win = ResultWindow(self._root, image, self._store, pending=True)
        t0 = time.perf_counter()
        f2 = self._executor.submit(_safe_recognize, self._recognizer, image, t0)
        f2.add_done_callback(lambda ff: self._bus.post(self._on_ocr_done, win, image, ff))

    def _on_ocr_done(self, win: ResultWindow, image_bgr: np.ndarray, fut) -> None:
        """主线程：识别完成 → 刷新结果窗文本；历史保存走子线程。"""
        results, elapsed = fut.result()
        if isinstance(results, Exception):
            win.set_error(f"Ошибка распознавания: {results}")
            return
        texts = [r.text for r in results if r.text and r.text.strip()]
        full_text = "\n".join(texts)
        if not full_text:
            win.set_empty(elapsed)  # 空态：窗口内提示，不保存历史（Spec §4.2）
            return
        score = (sum(r.score for r in results) / len(results)) if results else None
        box_count = len(results)
        win.set_result(full_text, score, elapsed)
        if self._config.auto_copy:
            win.copy()  # 主线程剪贴板（Tk 要求）
        # 历史保存（JSON+缩略图 I/O）放子线程，完成后刷新结果窗历史计数
        fut3 = self._executor.submit(self._save_history_job, full_text, score,
                                     image_bgr, box_count)
        fut3.add_done_callback(lambda f: self._bus.post(win.refresh_history))

    def _save_history_job(self, text: str, score: float | None,
                          image_bgr: np.ndarray, box_count: int) -> None:
        """子线程：写历史（单文件 history.json，含图片 base64；HistoryStore 内部有锁）。"""
        try:
            self._store.add(text, score, image_bgr, box_count)
        except Exception:  # noqa: BLE001 - 历史保存失败不崩应用
            logger.exception("历史保存失败")

    # =====================================================================
    # 托盘子窗口
    # =====================================================================

    def show_settings(self) -> None:
        SettingsWindow(self._root, self._config, self._on_settings_saved)

    def _on_settings_saved(self, new_cfg: Config) -> tuple[bool, str | None]:
        """保存设置：写盘 → 热键重注册 → 更新历史上限。返回 (ok, err_msg)。

        成功 (True, None)；失败 (False, 原因)，SettingsWindow 保留输入（Spec §4.3 异常态）。
        模型/阈值/批次有变更时，立即在后台按新配置重新加载引擎（完成后提示）。
        """
        try:
            config_mod.save(new_cfg)
        except OSError as exc:
            logger.exception("配置保存失败")
            return False, f"Не удалось сохранить: {exc}"
        hotkey_changed = new_cfg.hotkey != self._config.hotkey
        self._config = new_cfg
        self._store.set_limit(new_cfg.history_limit)
        if hotkey_changed:
            self._unregister_hotkey()
            self._register_hotkey()
        # 应用前处理（即时生效）
        self._recognizer.set_preprocess(new_cfg.preprocess_scale,
                                        new_cfg.preprocess_padding)
        # 模型/阈值/批次：有变更则立即按新配置重新加载引擎（后台，不阻塞 UI）
        model_changed = self._recognizer.configure_models(
            det_model_path=new_cfg.ocr_det_model,
            rec_model_path=new_cfg.ocr_rec_model,
            rec_char_dict_path=new_cfg.ocr_char_dict,
            det_thresh=new_cfg.ocr_det_thresh,
            det_box_thresh=new_cfg.ocr_det_box_thresh,
            det_unclip_ratio=new_cfg.ocr_det_unclip_ratio,
            rec_batch_size=new_cfg.ocr_rec_batch_size,
        )
        if model_changed:
            fut = self._executor.submit(self._recognizer.reload)
            fut.add_done_callback(
                lambda f: self._bus.post(self._on_model_reloaded, f))
        logger.info("设置已保存: %s", new_cfg.hotkey)
        return True, None

    def _on_model_reloaded(self, fut) -> None:
        """主线程：模型重载完成/失败提示（经 EventBus 派发）。"""
        ok, err = fut.result()
        if ok:
            show_notice(self._root, "Настройки",
                        "Модель перезагружена с новыми параметрами")
        else:
            show_notice(self._root, "Настройки",
                        f"Не удалось загрузить модель: {err}",
                        auto_close_ms=4000)

    def show_history(self) -> None:
        HistoryWindow(self._root, self._store)

    # =====================================================================
    # 退出（唯一真正结束进程的路径）
    # =====================================================================

    def shutdown(self) -> None:
        logger.info("SnapOCR 退出中…")
        self._unregister_hotkey()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:  # noqa: BLE001
                pass
        self._recognizer.close()
        if self._root.winfo_exists():
            self._root.destroy()


# ---------------------------------------------------------------------------
# 子线程任务（在 executor 中执行；返回异常对象而非抛出，避免跨线程异常丢失）
# ---------------------------------------------------------------------------


def _safe_grab(background, result: CaptureResult):
    """子线程裁选区：从全屏背景裁出选区内容，返回 BGR ndarray。

    背景截图失败（background=None）时回退为按坐标直接截屏；旋转选区返回
    「摆正」后的内容；异形框（result.quad）返回透视矫正后的矩形内容；
    失败返回异常对象（由 _on_grabbed 统一提示）。
    """
    try:
        if background is not None:
            return extract_region(background, result)
        if result.quad is not None:
            from .capture import grab_screen, outer_bbox, warp_quad
            x0, y0, x1, y1 = outer_bbox(result.quad)
            x0, y0 = max(0, x0), max(0, y0)
            img = grab_screen((x0, y0, max(32, x1 - x0), max(32, y1 - y0)))
            local = [(x - x0, y - y0) for x, y in result.quad]
            return warp_quad(img, local)
        cx, cy, w, h = result.cx, result.cy, result.w, result.h
        return grab_screen((max(0, int(round(cx - w / 2))),
                            max(0, int(round(cy - h / 2))),
                            max(32, w), max(32, h)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("截图失败")
        return exc


def _safe_recognize(recognizer: OCRRecognizer, image: np.ndarray, t0: float):
    """子线程识别：返回 (results, elapsed)；异常时 (exc, elapsed)。"""
    try:
        results = recognizer.recognize(image)
        return results, time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001 - 错误态：应用不崩溃
        logger.exception("OCR 识别异常")
        return exc, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# 托盘图标
# ---------------------------------------------------------------------------


def _make_tray_image() -> Image.Image:
    """生成 64×64 托盘图标（蓝底 + "OCR"）。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill="#2b7de9")
    font = _load_font(20)
    text = "OCR"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((64 - tw) / 2 - bbox[0], 22 - bbox[1]), text, fill="white", font=font)
    return img


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 旧版 Pillow 无 size 参数
        return ImageFont.load_default()
