"""ui —— 三个 Tk 窗口：结果 / 设置 / 历史。

对应 DESIGN §2.5：
    - ResultWindow（Page_Result）：源图缩略 + 可编辑文本框 + 复制/关闭 + Esc
    - SettingsWindow（Page_Settings）：热键录入 + 自动复制 + 语言只读 + 历史上限 + 保存/取消
    - HistoryWindow（Page_History）：复用结果窗口图+文布局 + 翻页条（上一页/下一页/上5页/下5页）+ 删除当前/清空全部（危险操作二次确认）

所有窗口为 Toplevel，仅在本模块主线程创建/操作（EventBus 保证）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

from ..config import Config
from ..cores.logger import get_logger
from .history import HistoryStore

logger = get_logger(__name__)

_FONT = ("Segoe UI", 10)
_HINT = "Скопировано"
_WARN_TITLE = "Подтверждение"


def _copy_to_clipboard(root: tk.Misc, text: str) -> None:
    """复制文本到剪贴板（Tk 需保持 clipboard 所有权：clear + append + update）。"""
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()


def _format_elapsed(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds >= 1.0 else f"{seconds * 1000:.0f}ms"


def show_notice(root: tk.Tk, title: str, message: str,
                auto_close_ms: int = 1500) -> tk.Toplevel:
    """独立提示窗（非阻塞，不依赖 root 可见性）。

    应用常驻托盘、root 隐藏，messagebox(parent=隐藏root) 在 Windows 上不可见/
    阻塞（原生模态对话框会卡住 Tcl 事件循环）。故统一用独立 Toplevel 提示。
    返回窗口句柄，便于外部引用/测试。
    """
    win = tk.Toplevel(root)
    win.title(title)
    win.attributes("-toolwindow", True)
    win.geometry("560x160")
    win.resizable(False, False)
    win.update_idletasks()
    # 居中于屏幕
    x = (root.winfo_screenwidth() - 560) // 2
    y = (root.winfo_screenheight() - 160) // 2
    win.geometry(f"560x160+{x}+{y}")
    win.lift()
    win.focus_force()

    body = ttk.Frame(win, padding=14)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, wraplength=520, justify="center").pack(fill="x")
    btn = ttk.Button(body, text="Закрыть", command=win.destroy)
    btn.pack(pady=(10, 0))

    if auto_close_ms > 0:
        win.after(auto_close_ms, lambda: win.destroy() if win.winfo_exists() else None)
    return win


# ---------------------------------------------------------------------------
# Page_Result —— 识别结果窗口（Spec §4.2）
# ---------------------------------------------------------------------------


class ResultWindow:
    """识别结果窗口：左 1/3 源图 + 右 2/3 可编辑文本 + 历史记录翻页。

    - 截图完成即弹出（图片 + “识别中…”），识别完成后 set_result() 刷新
    - 左侧源图占窗口 1/3，随窗口自适应等比缩放；**双击图片**打开独立看图窗口放大
    - 文本区头部一个「复制全文」按钮（唯一复制入口）
    - 底栏为单一动作条：live 显示历史条数 + 关闭；history 显示
      上一页/下一页/上5页/下5页 翻页 + 位置 + 关闭（删除/清空由 HistoryWindow 追加）
    Alias: Page_Result / View_SourceImage / Edit_TextBox / Btn_CopyText / Btn_CloseResult
    """

    _ST_PENDING = "pending"
    _ST_RESULT = "result"
    _ST_EMPTY = "empty"
    _ST_ERROR = "error"

    _FAST_STEP = 5         # 快上/快下翻 5 条（上5页/下5页）

    def __init__(self, root: tk.Tk, image_bgr: np.ndarray, store: "HistoryStore",
                 pending: bool = True, history_mode: bool = False,
                 title: str | None = None) -> None:
        """识别结果 / 历史浏览 两用窗口。

        history_mode=True 时：
          - 标题用「历史记录」（默认），并显示上一页/下一页/上5页/下5页 翻页条
          - 打开即定位到最新一条历史（若存在）
        非 history_mode（识别态）：
          - 标题「识别结果」，隐藏翻页条
        """
        self._root = root
        self._store = store
        self._history_mode = history_mode
        self._photo: ImageTk.PhotoImage | None = None
        self._source_pil: Image.Image | None = None   # 当前显示源图（RGB 原尺寸）
        self._live_image = image_bgr
        self._live_text = ""
        self._live_score: float | None = None
        self._live_elapsed = 0.0
        self._state = self._ST_PENDING
        self._mode = "live"          # live=当前识别 | history=浏览历史
        self._hist_entries: list = []
        self._hist_idx = 0

        win = tk.Toplevel(root)
        win.title(title or ("История" if history_mode else "Результат распознавания"))
        win.attributes("-toolwindow", True)   # 不进任务栏/Alt-Tab
        win.geometry("960x600")
        win.minsize(760, 460)
        self._win = win

        # 顶栏：标题 + 状态（顶层用 grid 布局 + 行权重撑开主体，
        # 避免主体把导航条/底栏/工具条挤出窗口——pack 在 body 内容很高时会挤掉后续 frame）
        win.columnconfigure(0, weight=1)
        self._row = 0
        top = ttk.Frame(win, padding=(10, 8))
        top.grid(row=self._row, column=0, sticky="ew")
        self._row += 1
        ttk.Label(top, text=win.title(),
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        self._status_lbl = ttk.Label(top, text="", foreground="#666")
        self._status_lbl.pack(side="right")

        # 主体：左 1/3 图 + 右 2/3 文本
        win.rowconfigure(self._row, weight=1)   # 主体行获得所有剩余高度
        body = ttk.Frame(win, padding=(10, 0))
        body.grid(row=self._row, column=0, sticky="nsew")
        self._row += 1
        body.columnconfigure(0, weight=1)   # 左 1/3：图片
        body.columnconfigure(1, weight=2)   # 右 2/3：文本
        body.rowconfigure(0, weight=1)

        # 左：自适应缩放源图（双击打开看图窗口）
        img_frame = ttk.LabelFrame(body, text="Снимок (двойной клик — увеличить)")
        img_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=4)
        img_frame.rowconfigure(0, weight=1)
        img_frame.columnconfigure(0, weight=1)
        img_canvas = tk.Canvas(img_frame, bg="#f2f2f2", highlightthickness=0,
                               cursor="hand2")
        img_canvas.grid(row=0, column=0, sticky="nsew")
        img_canvas.bind("<Configure>", self._on_img_resize)
        img_canvas.bind("<Double-Button-1>", lambda _e: self._open_viewer())
        self._img_canvas = img_canvas
        self._img_frame = img_frame

        # 右：文本（头部快捷复制）
        txt_frame = ttk.LabelFrame(body, text="Текст")
        txt_frame.grid(row=0, column=1, sticky="nsew")
        txt_frame.rowconfigure(1, weight=1)
        txt_frame.columnconfigure(0, weight=1)
        txt_header = ttk.Frame(txt_frame)
        txt_header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 0))
        ttk.Label(txt_header, text="Редактируемый текст", foreground="#666").pack(side="left")
        self._copy_btn = ttk.Button(txt_header, text="Копировать всё", width=16,
                                    command=self._on_copy, state="disabled")
        self._copy_btn.pack(side="right")
        text = tk.Text(txt_frame, wrap="word", font=_FONT, undo=True)
        text.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=4)
        scroll = ttk.Scrollbar(txt_frame, orient="vertical", command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=4)
        text.configure(yscrollcommand=scroll.set)
        self._text = text

        # 底栏：单一动作条
        #   live 模式：左侧「共 N 条」计数 + 右侧 [关闭]
        #   history 模式：左侧 [上一页][下一页][上5页][下5页] [位置] + 右侧 [关闭]
        #   （HistoryWindow 再在左侧追加 [删除当前][清空全部]）
        bottom = ttk.Frame(win, padding=(10, 6))
        bottom.grid(row=self._row, column=0, sticky="ew")
        self._row += 1
        self._bottom = bottom
        if history_mode:
            for label, cmd in (("Назад", self._go_prev), ("Вперёд", self._go_next),
                               ("-5", self._go_prev_fast), ("+5", self._go_next_fast)):
                ttk.Button(bottom, text=label, width=8, command=cmd).pack(
                    side="left", padx=2)
            self._nav_lbl = ttk.Label(bottom, text="", foreground="#666")
            self._nav_lbl.pack(side="left", padx=(10, 2))
        else:
            self._nav_lbl = ttk.Label(bottom, text="", foreground="#666")
            self._nav_lbl.pack(side="left")
        ttk.Button(bottom, text="Закрыть", command=self._on_close).pack(side="right")

        win.bind("<Escape>", lambda _e: self._on_close())
        win.protocol("WM_DELETE_WINDOW", self._on_close)

        # 截图完成立即显示源图（左 1/3），文本区先显示"识别中…"
        self._render_image(image_bgr)
        if history_mode:
            self._open_latest_history()
        elif pending:
            self._set_pending()

    # -- 状态刷新（均由主线程经 EventBus 调用） ----------------------------

    def _set_pending(self) -> None:
        self._state = self._ST_PENDING
        self._write_text("Распознавание…", editable=False)
        self._status_lbl.configure(text="Распознавание…")

    def set_result(self, text: str, score: float | None, elapsed: float) -> None:
        """识别完成：回到 live 视图，刷新为可编辑全文并启用复制。"""
        if not self._win.winfo_exists():
            return
        self._live_text = text
        self._live_score = score
        self._live_elapsed = elapsed
        self._state = self._ST_RESULT
        self._mode = "live"
        self._render_image(self._live_image)
        self._write_text(text, editable=True)
        self._copy_btn.configure(state="normal")
        self._refresh_status()
        self._nav_lbl.configure(text="")  # live 模式条数由 refresh_history 填充

    def set_empty(self, elapsed: float) -> None:
        """空态：未检测到文字，不保存历史（Spec §4.2 状态与异常态）。"""
        if not self._win.winfo_exists():
            return
        self._state = self._ST_EMPTY
        self._write_text("Текст не найден", editable=False)
        self._status_lbl.configure(text=f"Время: {_format_elapsed(elapsed)}")

    def set_error(self, message: str) -> None:
        """错误态：在窗口内展示错误，应用不崩溃、托盘仍可用。"""
        if not self._win.winfo_exists():
            return
        self._state = self._ST_ERROR
        self._write_text(f"Ошибка распознавания: {message}", editable=False)
        self._status_lbl.configure(text="Ошибка")

    def copy(self) -> None:
        """复制当前文本框内容并提示（自动复制 / 手动复制共用，主线程调用）。"""
        if self._state != self._ST_RESULT:
            return
        content = self._text.get("1.0", "end-1c")
        if not content.strip():
            return
        _copy_to_clipboard(self._root, content)
        self._flash(_HINT)

    def refresh_history(self) -> None:
        """历史保存完成后刷新计数（不打断当前显示；live 态显示条数）。"""
        if not self._win.winfo_exists():
            return
        try:
            count = self._store.count()
        except Exception:  # noqa: BLE001
            return
        if self._mode == "live":
            self._nav_lbl.configure(text=f"Всего: {count}")

    # -- 历史翻页 ---------------------------------------------------------

    def _open_latest_history(self) -> None:
        """历史模式：打开即定位到最新一条（若无历史则提示空态）。"""
        self._reload_entries()
        if not self._hist_entries:
            self._mode = "history"
            self._state = self._ST_EMPTY
            self._write_text("История пуста", editable=False)
            self._status_lbl.configure(text="Пусто")
            self._nav_lbl.configure(text="0/0")
            return
        self._hist_idx = 0
        self._show_history_entry(0)

    def _reload_entries(self) -> None:
        try:
            self._hist_entries = self._store.list_entries()
        except Exception:  # noqa: BLE001
            self._hist_entries = []

    def _go_prev(self) -> None:
        """上一条（更旧）。live → 最新一条历史；history → 更旧。"""
        self._reload_entries()
        if not self._hist_entries:
            self._flash("История пуста")
            return
        if self._mode == "live":
            self._hist_idx = 0
        else:
            self._hist_idx = min(self._hist_idx + 1, len(self._hist_entries) - 1)
        self._show_history_entry(self._hist_idx)

    def _go_next(self) -> None:
        """下一页（更新）。到达最新则停在最新；识别态(live)无更下一项。"""
        self._reload_entries()
        if not self._hist_entries:
            self._flash("История пуста")
            return
        if self._mode == "live":
            return  # 已是最新（识别态无更下一项）
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._show_history_entry(self._hist_idx)
        else:
            self._show_history_entry(0)  # 已在最新

    def _go_prev_fast(self) -> None:
        """快上：向更旧跳上5页。"""
        self._reload_entries()
        if not self._hist_entries:
            self._flash("История пуста")
            return
        if self._mode == "live":
            self._hist_idx = min(self._FAST_STEP, len(self._hist_entries) - 1)
        else:
            self._hist_idx = min(self._hist_idx + self._FAST_STEP,
                                 len(self._hist_entries) - 1)
        self._show_history_entry(self._hist_idx)

    def _go_next_fast(self) -> None:
        """快下：向更新跳下5页；已在最新则停在最新。"""
        self._reload_entries()
        if not self._hist_entries:
            self._flash("История пуста")
            return
        if self._mode == "live":
            return
        self._hist_idx = max(self._hist_idx - self._FAST_STEP, 0)
        self._show_history_entry(self._hist_idx)

    def _show_history_entry(self, idx: int) -> None:
        entry = self._hist_entries[idx]
        self._mode = "history"
        self._state = self._ST_RESULT
        # 图片内嵌在 history.json 中（base64），解码后展示；失败提示缺失
        self._set_source_image(self._store.load_image(entry))
        self._write_text(entry.text, editable=True)
        self._copy_btn.configure(state="normal")
        score_txt = f" · Уверенность {entry.score:.2f}" if entry.score is not None else ""
        self._status_lbl.configure(text=f"{entry.ts}{score_txt}")
        self._nav_lbl.configure(text=f"{idx + 1}/{len(self._hist_entries)}")

    def _show_live(self) -> None:
        self._mode = "live"
        self._render_image(self._live_image)
        self._write_text(self._live_text, editable=True)
        self._copy_btn.configure(state="normal")
        self._refresh_status()
        self._nav_lbl.configure(text="")

    # -- 内部 -------------------------------------------------------------

    def _render_image(self, image_bgr: np.ndarray) -> None:
        """设置当前源图为识别/实时截图（BGR → RGB 原尺寸），并按画布适配。"""
        self._source_pil = Image.fromarray(np.asarray(image_bgr)[:, :, ::-1])
        self._fit_image()

    def _set_source_image(self, pil: Image.Image | None) -> None:
        """设置当前源图为 PIL 图（历史解码结果）；None 则在画布提示缺失。"""
        if pil is None:
            self._source_pil = None
            canvas = self._img_canvas
            canvas.delete("all")
            canvas.create_text(canvas.winfo_width() // 2, canvas.winfo_height() // 2,
                               text="Изображение отсутствует", fill="#999")
            return
        self._source_pil = pil
        self._fit_image()

    def _fit_image(self) -> None:
        """把当前源图等比缩放到画布（左 1/3 列）并居中显示。"""
        canvas = self._img_canvas
        if self._source_pil is None or not canvas.winfo_exists():
            return
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw < 10 or ch < 10:
            self._win.after(30, self._fit_image)  # 窗口尚未布局，稍后再适配
            return
        iw, ih = self._source_pil.size
        if iw == 0 or ih == 0:
            return
        scale = min(cw / iw, ch / ih)
        w = max(1, int(iw * scale))
        h = max(1, int(ih * scale))
        img = self._source_pil.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        canvas.delete("all")
        canvas.create_image(cw // 2, ch // 2, image=self._photo)
        canvas.image = self._photo  # 防 GC

    def _on_img_resize(self, _event: tk.Event) -> None:
        """窗口/左列尺寸变化时重新适配源图。"""
        if self._source_pil is not None:
            self._fit_image()

    def _open_viewer(self) -> ImageViewerWindow | None:
        """双击左图：打开独立看图窗口（滚轮缩放 / 拖拽平移 / 双击切换）。"""
        if self._source_pil is None or not self._win.winfo_exists():
            return None
        return ImageViewerWindow(self._root, self._source_pil.copy(), title="Просмотр изображения")

    def _refresh_status(self) -> None:
        elapsed_txt = f"Время: {_format_elapsed(self._live_elapsed)}"
        if self._live_score is not None:
            elapsed_txt += f" | Средняя уверенность {self._live_score:.2f}"
        self._status_lbl.configure(text=elapsed_txt)

    def _write_text(self, content: str, editable: bool) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)
        self._text.configure(state="normal" if editable else "disabled")

    def _on_copy(self) -> None:
        self.copy()

    def _on_close(self) -> None:
        if self._win.winfo_exists():
            self._win.destroy()

    def _flash(self, text: str) -> None:
        self._copy_btn.configure(text=text)
        self._win.after(1200, lambda: self._copy_btn.configure(text="Копировать всё"))


# ---------------------------------------------------------------------------
# ImageViewerWindow —— 独立看图窗口（结果窗左图双击打开）
# ---------------------------------------------------------------------------


class ImageViewerWindow:
    """独立看图窗口：滚轮缩放、拖拽平移、双击切换、+/- 缩放。

    操作习惯对齐常见看图软件：
        - 滚轮：以光标为中心放大/缩小
        - 左键拖拽：平移（放大后）
        - 双击：在「适应窗口 ↔ 实际大小」间切换
        - 工具栏：放大 / 缩小 / 适应窗口 / 实际大小 / 关闭
        - 快捷键：+ / - 缩放，0 适应窗口，Esc 关闭
    """

    _MIN_SCALE = 0.05
    _MAX_SCALE = 20.0
    _WHEEL_STEP = 1.25

    def __init__(self, root: tk.Tk, image: Image.Image, title: str = "Просмотр изображения") -> None:
        self._root = root
        self._source = image.convert("RGB")
        self._photo: ImageTk.PhotoImage | None = None
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._fit_mode = True
        self._pan_start: tuple[int, int, float, float] | None = None

        win = tk.Toplevel(root)
        win.title(title)
        win.geometry("920x680")
        win.minsize(640, 400)
        self._win = win

        # 工具栏
        bar = ttk.Frame(win, padding=(8, 6))
        bar.pack(fill="x")
        for text, cmd in (("Крупнее", self._zoom_in), ("Мельче", self._zoom_out),
                          ("Вписать", self._set_fit), ("1:1", self._set_100),
                          ("Закрыть", self._on_close)):
            ttk.Button(bar, text=text, width=9, command=cmd).pack(side="left", padx=3)
        self._info_lbl = ttk.Label(bar, text="", foreground="#666")
        self._info_lbl.pack(side="right", padx=6)

        # 画布
        canvas = tk.Canvas(win, bg="#222222", highlightthickness=0, cursor="fleur")
        canvas.pack(fill="both", expand=True)
        self._canvas = canvas

        canvas.bind("<Configure>", self._on_resize)
        canvas.bind("<MouseWheel>", self._on_wheel)
        canvas.bind("<Button-4>", lambda _e: self._zoom_center(self._WHEEL_STEP))
        canvas.bind("<Button-5>", lambda _e: self._zoom_center(1 / self._WHEEL_STEP))
        canvas.bind("<ButtonPress-1>", self._on_pan_start)
        canvas.bind("<B1-Motion>", self._on_pan)
        canvas.bind("<Double-Button-1>", self._on_double)
        win.bind("<Escape>", lambda _e: self._on_close())
        win.bind("<KeyPress-plus>", lambda _e: self._zoom_in())
        win.bind("<KeyPress-equal>", lambda _e: self._zoom_in())
        win.bind("<KeyPress-minus>", lambda _e: self._zoom_out())
        win.bind("<KeyPress-0>", lambda _e: self._set_fit())
        win.protocol("WM_DELETE_WINDOW", self._on_close)

        win.update_idletasks()
        win.after(30, self._set_fit)  # 等窗口完成布局后按画布尺寸适配

    # -- 缩放 -------------------------------------------------------------

    def _zoom_in(self) -> None:
        self._zoom_center(self._WHEEL_STEP)

    def _zoom_out(self) -> None:
        self._zoom_center(1 / self._WHEEL_STEP)

    def _zoom_center(self, factor: float) -> None:
        c = self._canvas
        self._zoom(factor, c.winfo_width() / 2, c.winfo_height() / 2)

    def _on_wheel(self, event: tk.Event) -> None:
        step = self._WHEEL_STEP ** (event.delta / 120) if event.delta else self._WHEEL_STEP
        self._zoom(step, event.x, event.y)
        return "break"  # 阻止滚动冒泡

    def _zoom(self, factor: float, cx: float, cy: float) -> None:
        """以 (cx, cy) 为不动点缩放。"""
        new_scale = min(self._MAX_SCALE, max(self._MIN_SCALE, self._scale * factor))
        if abs(new_scale - self._scale) < 1e-9:
            return
        # 保持光标下的图像点不动
        px = (cx - self._ox) / self._scale
        py = (cy - self._oy) / self._scale
        self._scale = new_scale
        self._ox = cx - px * new_scale
        self._oy = cy - py * new_scale
        self._fit_mode = False
        self._clamp_offsets()
        self._draw()

    # -- 平移 -------------------------------------------------------------

    def _on_pan_start(self, event: tk.Event) -> None:
        self._pan_start = (event.x, event.y, self._ox, self._oy)

    def _on_pan(self, event: tk.Event) -> None:
        if self._pan_start is None:
            return
        x0, y0, ox0, oy0 = self._pan_start
        self._ox = ox0 + (event.x - x0)
        self._oy = oy0 + (event.y - y0)
        self._fit_mode = False
        self._clamp_offsets()
        self._draw()

    def _on_double(self, _event: tk.Event) -> None:
        """双击：适应窗口 ↔ 实际大小 切换。"""
        if self._fit_mode:
            self._set_100()
        else:
            self._set_fit()

    # -- 适配 -------------------------------------------------------------

    def _set_fit(self) -> None:
        c = self._canvas
        if not c.winfo_exists():
            return
        cw, ch = c.winfo_width(), c.winfo_height()
        iw, ih = self._source.size
        if cw < 10 or ch < 10 or iw == 0 or ih == 0:
            return
        scale = min(cw / iw, ch / ih)
        self._scale = scale
        self._ox = (cw - iw * scale) / 2
        self._oy = (ch - ih * scale) / 2
        self._fit_mode = True
        self._draw()

    def _set_100(self) -> None:
        c = self._canvas
        if not c.winfo_exists():
            return
        cw, ch = c.winfo_width(), c.winfo_height()
        iw, ih = self._source.size
        self._scale = 1.0
        self._ox = (cw - iw) / 2
        self._oy = (ch - ih) / 2
        self._fit_mode = False
        self._clamp_offsets()
        self._draw()

    def _on_resize(self, _event: tk.Event) -> None:
        if self._fit_mode:
            self._set_fit()
        else:
            self._clamp_offsets()
            self._draw()

    def _clamp_offsets(self) -> None:
        """防止放大后图像完全移出视野。"""
        c = self._canvas
        if not c.winfo_exists():
            return
        cw, ch = c.winfo_width(), c.winfo_height()
        iw, ih = self._source.size
        w = iw * self._scale
        h = ih * self._scale
        if w <= cw:
            self._ox = (cw - w) / 2
        else:
            self._ox = min(0, max(cw - w, self._ox))
        if h <= ch:
            self._oy = (ch - h) / 2
        else:
            self._oy = min(0, max(ch - h, self._oy))

    # -- 绘制 -------------------------------------------------------------

    def _draw(self) -> None:
        c = self._canvas
        if not c.winfo_exists():
            return
        c.delete("img")
        iw, ih = self._source.size
        w = max(1, int(round(iw * self._scale)))
        h = max(1, int(round(ih * self._scale)))
        img = self._source.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        c.create_image(self._ox, self._oy, anchor="nw", image=self._photo, tags="img")
        self._info_lbl.configure(text=f"{iw}×{ih}  Масштаб {self._scale * 100:.0f}%")

    def _on_close(self) -> None:
        if self._win.winfo_exists():
            self._win.destroy()


# ---------------------------------------------------------------------------
# Page_Settings —— 设置窗口（Spec §4.3）
# ---------------------------------------------------------------------------


class SettingsWindow:
    """设置：热键录入 / 自动复制 / 语言只读 / 历史上限。

    Alias: Page_Settings / Input_Hotkey / Chk_AutoCopy / Lbl_Language /
           Spin_HistoryLimit / Btn_SaveSettings / Btn_CancelSettings
    保存成功后回调 on_save(new_config)（App 负责持久化 + 热键重注册）。
    """

    # Tk event.state 掩码
    _ST_CTRL = 0x0004
    _ST_SHIFT = 0x0001
    _ST_ALT = 0x20000
    _ST_WIN = 0x40000

    _MODIFIER_KEYSYMS = {
        "control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r",
        "super_l", "super_r", "win_l", "win_r", "caps_lock", "num_lock",
    }

    def __init__(self, root: tk.Tk, config: Config, on_save) -> None:
        self._root = root
        self._on_save_cb = on_save
        self._config = Config(**vars(config))  # 副本，避免未保存就污染

        win = tk.Toplevel(root)
        win.title("Настройки")
        win.attributes("-toolwindow", True)
        win.geometry("660x640")
        win.minsize(580, 480)
        self._win = win

        pad = {"padx": 12, "pady": 6}
        # 可滚动主体（新分组较多，避免超出屏幕）
        outer = ttk.Frame(win)
        outer.pack(fill="both", expand=True, padx=8, pady=4)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            body_id, width=e.width))
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))
        self._body = body

        # ===== 通用 =====
        ttk.Label(body, text="Общие", font=("Segoe UI", 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(2, 0))
        row1 = ttk.Frame(body)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Горячая клавиша:", width=20).pack(side="left")
        self._hotkey_var = tk.StringVar(value=self._display_hotkey(config.hotkey))
        entry = ttk.Entry(row1, textvariable=self._hotkey_var, state="readonly",
                          width=22)
        entry.pack(side="left")
        entry.bind("<KeyPress>", self._on_hotkey_key)
        self._hotkey_entry = entry

        self._auto_copy_var = tk.BooleanVar(value=config.auto_copy)
        ttk.Checkbutton(body, text="Автоматически копировать в буфер обмена",
                        variable=self._auto_copy_var).pack(anchor="w", **pad)

        style_frame = ttk.Frame(body)
        style_frame.pack(fill="x", **pad)
        ttk.Label(style_frame, text="Стиль выделения:", width=20).pack(side="left")
        self._overlay_style_var = tk.StringVar(
            value=config.overlay_style if config.overlay_style in ("bright", "dark")
            else "bright")
        ttk.Radiobutton(style_frame, text="Светлый (область яркая, фон затемнён)",
                        variable=self._overlay_style_var, value="bright"
                        ).pack(side="left")
        ttk.Radiobutton(style_frame, text="Полупрозрачный серый",
                        variable=self._overlay_style_var, value="dark"
                        ).pack(side="left")

        lang_frame = ttk.Frame(body)
        lang_frame.pack(fill="x", **pad)
        ttk.Label(lang_frame, text="Язык распознавания:", width=20).pack(side="left")
        ttk.Label(lang_frame, text="Китайский + английский", foreground="#666").pack(side="left")

        limit_frame = ttk.Frame(body)
        limit_frame.pack(fill="x", **pad)
        ttk.Label(limit_frame, text="Лимит истории:", width=20).pack(side="left")
        self._limit_var = tk.StringVar(value=str(config.history_limit))
        ttk.Spinbox(limit_frame, from_=10, to=50, textvariable=self._limit_var,
                    width=10).pack(side="left")
        ttk.Label(limit_frame, text="записей (10–50)", foreground="#666"
                  ).pack(side="left", padx=4)

        # ===== 前处理 =====
        ttk.Label(body, text="Предобработка OCR (только для распознавания; окно и история — оригинал)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=6,
                                                                pady=(8, 0))
        pre_scale = ttk.Frame(body)
        pre_scale.pack(fill="x", **pad)
        ttk.Label(pre_scale, text="Масштаб:", width=20).pack(side="left")
        self._scale_var = tk.StringVar(value=str(config.preprocess_scale))
        ttk.Spinbox(pre_scale, from_=10, to=500, increment=10,
                    textvariable=self._scale_var, width=8).pack(side="left")
        ttk.Label(pre_scale, text="% (100 = исходный размер; мелкий текст — увеличить)",
                  foreground="#666").pack(side="left", padx=4)

        pre_pad = ttk.Frame(body)
        pre_pad.pack(fill="x", **pad)
        ttk.Label(pre_pad, text="Отступ по краям:", width=20).pack(side="left")
        self._padding_var = tk.StringVar(value=str(config.preprocess_padding))
        ttk.Spinbox(pre_pad, from_=0, to=200, increment=5,
                    textvariable=self._padding_var, width=8).pack(side="left")
        ttk.Label(pre_pad, text="пикс. (текст у краёв распознаётся лучше)",
                  foreground="#666").pack(side="left", padx=4)

        # ===== 模型参数 =====
        ttk.Label(body, text="Параметры моделей (пусто = встроенная модель)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=6,
                                                                pady=(8, 0))
        self._model_vars = {}
        for key, label, ftype in (("det", "Модель детекции (det.onnx)", [("ONNX", "*.onnx")]),
                                  ("rec", "Модель распознавания (rec.onnx)", [("ONNX", "*.onnx")]),
                                  ("dict", "Словарь (chars.txt)", [("Текст", "*.txt")])):
            row = ttk.Frame(body)
            row.pack(fill="x", **pad)
            ttk.Label(row, text=label, width=32).pack(side="left")
            var = tk.StringVar(value=getattr(config, f"ocr_{key}_model" if
                                             key != "dict" else "ocr_char_dict"))
            ttk.Entry(row, textvariable=var, width=26).pack(side="left", padx=(0, 4))
            ttk.Button(row, text="Обзор…", width=8,
                       command=lambda k=key, f=ftype: self._browse(k, f)).pack(
                side="left")
            self._model_vars[key] = var

        # ===== 检测阈值 =====
        ttk.Label(body, text="Пороги детекции (тонкая настройка при неточном распознавании)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=6,
                                                                pady=(8, 0))
        self._thresh_vars = {}
        thresh_specs = (
            ("ocr_det_thresh", "Порог бинаризации", 0.0, 1.0, 0.05),
            ("ocr_det_box_thresh", "Порог рамки", 0.0, 1.0, 0.05),
            ("ocr_det_unclip_ratio", "Коэф. расширения", 1.0, 10.0, 0.1),
        )
        for field, label, lo, hi, step in thresh_specs:
            row = ttk.Frame(body)
            row.pack(fill="x", **pad)
            ttk.Label(row, text=label, width=20).pack(side="left")
            var = tk.StringVar(value=str(getattr(config, field)))
            ttk.Spinbox(row, from_=lo, to=hi, increment=step,
                        textvariable=var, width=8).pack(side="left")
            self._thresh_vars[field] = var

        # ===== 识别批次 =====
        bs_row = ttk.Frame(body)
        bs_row.pack(fill="x", **pad)
        ttk.Label(bs_row, text="Размер батча:", width=20).pack(side="left")
        self._batch_var = tk.StringVar(value=str(config.ocr_rec_batch_size))
        ttk.Spinbox(bs_row, from_=1, to=32, textvariable=self._batch_var,
                    width=8).pack(side="left")
        ttk.Label(bs_row, text="(компромисс память/скорость)", foreground="#666"
                  ).pack(side="left", padx=4)

        # 按钮
        btns = ttk.Frame(win)
        btns.pack(fill="x", pady=(6, 12), padx=12)
        ttk.Button(btns, text="Сохранить", command=self._on_save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self._on_cancel).pack(side="right", padx=8)

        win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        entry.focus_set()

    # -- 文件浏览 ---------------------------------------------------------

    def _browse(self, key: str, ftypes) -> None:
        title = "Выбор модели детекции" if key == "det" else (
            "Выбор модели распознавания" if key == "rec" else "Выбор файла словаря")
        path = filedialog.askopenfilename(
            parent=self._win, title=title,
            filetypes=[*ftypes, ("Все файлы", "*.*")])
        if path:
            self._model_vars[key].set(path)

    # -- 动作 -------------------------------------------------------------

    def _get_bounded(self, var: tk.StringVar, lo: float, hi: float,
                     name: str) -> float:
        """解析数值并校验范围；非法抛 ValueError。"""
        try:
            val = float(var.get().strip())
        except ValueError:
            raise ValueError(f"{name}: требуется число") from None
        if not (lo <= val <= hi):
            raise ValueError(f"{name}: значение от {lo:g} до {hi:g}")
        return val

    def _on_save(self) -> None:
        from ..cores.hotkey import parse_hotkey

        hotkey = self._hotkey_var.get().strip().lower()
        if not hotkey or parse_hotkey(hotkey) is None:
            messagebox.showerror("Настройки", "Недопустимая комбинация: введите сочетание клавиш (например, Ctrl+Alt+Z)",
                                 parent=self._win)
            return
        try:
            limit = int(self._limit_var.get())
            if not (10 <= limit <= 50):
                raise ValueError
        except ValueError:
            messagebox.showerror("Настройки", "Лимит истории — целое число от 10 до 50", parent=self._win)
            return

        try:
            scale = int(self._get_bounded(self._scale_var, 10, 500, "Масштаб"))
            padding = int(self._get_bounded(self._padding_var, 0, 200, "Отступ по краям"))
            det_thresh = self._get_bounded(self._thresh_vars["ocr_det_thresh"],
                                           0.0, 1.0, "Порог бинаризации")
            box_thresh = self._get_bounded(
                self._thresh_vars["ocr_det_box_thresh"], 0.0, 1.0, "Порог рамки")
            unclip = self._get_bounded(self._thresh_vars["ocr_det_unclip_ratio"],
                                       1.0, 10.0, "Коэф. расширения")
            batch = int(self._get_bounded(self._batch_var, 1, 32, "Размер батча"))
        except ValueError as exc:
            messagebox.showerror("Настройки", str(exc), parent=self._win)
            return

        new_cfg = Config(
            hotkey=hotkey,
            auto_copy=bool(self._auto_copy_var.get()),
            language=self._config.language,
            history_limit=limit,
            overlay_style=self._overlay_style_var.get(),
            overlay_opacity=self._config.overlay_opacity,
            preprocess_scale=scale,
            preprocess_padding=padding,
            ocr_det_model=self._model_vars["det"].get().strip(),
            ocr_rec_model=self._model_vars["rec"].get().strip(),
            ocr_char_dict=self._model_vars["dict"].get().strip(),
            ocr_det_thresh=det_thresh,
            ocr_det_box_thresh=box_thresh,
            ocr_det_unclip_ratio=unclip,
            ocr_rec_batch_size=batch,
        )
        # on_save 返回 (ok, msg)：成功 → 提示"已保存"并关闭；失败 → 提示并保留输入
        ok, msg = self._on_save_cb(new_cfg)
        if ok:
            show_notice(self._root, "Настройки", "Сохранено", auto_close_ms=1200)
            if self._win.winfo_exists():
                self._win.destroy()
        else:
            show_notice(self._root, "Настройки", msg or "Не удалось сохранить, попробуйте ещё раз",
                        auto_close_ms=2500)

    def _on_cancel(self) -> None:
        if self._win.winfo_exists():
            self._win.destroy()

    # -- 热键录入 ---------------------------------------------------------

    def _on_hotkey_key(self, event: tk.Event) -> None:
        """聚焦后按组合键捕获并回显（Spec §4.3 Input_Hotkey）。"""
        keysym = (event.keysym or "").lower()
        if keysym in self._MODIFIER_KEYSYMS or keysym in ("escape",):
            return  # 纯修饰键/取消键不构成组合
        parts = []
        st = event.state
        if st & self._ST_CTRL:
            parts.append("ctrl")
        if st & self._ST_ALT:
            parts.append("alt")
        if st & self._ST_SHIFT:
            parts.append("shift")
        if st & self._ST_WIN:
            parts.append("win")
        parts.append(self._normalize_keysym(keysym))
        self._hotkey_var.set("+".join(parts))
        return "break"  # 阻止 Entry 默认插入

    @staticmethod
    def _normalize_keysym(keysym: str) -> str:
        key = keysym.lower()
        mapping = {"space": "space", "return": "enter"}
        if key in mapping:
            return mapping[key]
        return key

    @staticmethod
    def _display_hotkey(hotkey: str) -> str:
        from ..cores.hotkey import parse_hotkey

        spec = parse_hotkey(hotkey)
        if spec is None:
            return hotkey
        return "+".join(p.capitalize() for p in spec.display.split("+"))


# ---------------------------------------------------------------------------
# Page_History —— 历史窗口（Spec §4.4，识别样式浏览）
# ---------------------------------------------------------------------------


class HistoryWindow(ResultWindow):
    """历史浏览：复用识别结果窗口的图+文布局，标题「历史记录」。

    - 打开即定位到最新一条；底栏左侧为 上一页/下一页/上5页/下5页 翻页 + 位置
    - 同条底栏再提供「删除当前」「清空全部」（均二次确认，Spec §4.4）
    - 历史已保存原尺寸图，浏览时左侧直接显示截图
    """

    def __init__(self, root: tk.Tk, store: HistoryStore) -> None:
        # 历史模式下 image_bgr 仅作占位，实际由 history 文件加载
        import numpy as np
        placeholder = np.zeros((1, 1, 3), dtype=np.uint8)
        super().__init__(root, placeholder, store, pending=False,
                         history_mode=True, title="История")

        # 底栏左侧追加危险操作按钮（翻页按钮已由父类放在同一底栏）
        ttk.Button(self._bottom, text="Удалить текущую", command=self._delete_current).pack(
            side="left", padx=(14, 2))
        ttk.Button(self._bottom, text="Очистить всё", command=self._clear_all).pack(
            side="left", padx=2)

    # -- 数据管理（二次确认，Spec §4.4）-----------------------------------

    def _current_ts(self) -> str | None:
        idx = getattr(self, "_hist_idx", 0)
        entries = getattr(self, "_hist_entries", [])
        if not entries or idx >= len(entries):
            return None
        return entries[idx].ts

    def _delete_current(self) -> None:
        ts = self._current_ts()
        if not ts:
            self._flash("Нечего удалять")
            return
        if not messagebox.askyesno(_WARN_TITLE, "Удалить эту запись из истории?",
                                   parent=self._win):
            return
        self._store.delete(ts)
        self._refresh_nav()
        self._flash("Удалено")

    def _clear_all(self) -> None:
        if not messagebox.askyesno(_WARN_TITLE,
                                   "Очистить всю историю? Это действие необратимо.",
                                   parent=self._win):
            return
        self._store.clear()
        self._refresh_nav()
        self._flash("Очищено")

    def _refresh_nav(self) -> None:
        """删除/清空后回到最新一条（或空态）。"""
        self._open_latest_history()

    # 覆盖 ResultWindow.refresh_history：历史模式下刷新列表与位置（不打断当前条目）
    def refresh_history(self) -> None:
        if self._mode == "history":
            self._reload_entries()
            if self._hist_entries:
                self._hist_idx = min(self._hist_idx, len(self._hist_entries) - 1)
                self._nav_lbl.configure(
                    text=f"{self._hist_idx + 1}/{len(self._hist_entries)}")
