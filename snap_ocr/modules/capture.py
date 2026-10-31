"""capture —— 全屏框选遮罩 + 屏幕截图（支持选区移动/缩放/旋转 + 反旋转识别）。

实现（Spec §4.1 + 用户迭代）：
    - 流程：**先截全屏背景，再弹遮罩框选**（微信/QQ 等截图软件做法）。
      `App.start_capture` 先 `capture_background()` 截全屏保存，再 `CaptureOverlay`
      以该背景为底显示遮罩；确认后 `extract_region()` 直接裁全屏图 → 截图内容
      即按下热键那一刻的屏幕，不会包含遮罩本身，也不会因 Tk 遮罩半透明盖在
      下层窗口上而把变暗/遮挡效果一起截进去。
    - 遮罩风格（overlay_style，设置 → config.json）：
        * bright（默认）：打开时一次性预渲染整屏「压暗版」（numpy 亮度缩放 →
          **平滑半透明灰，无 stipple 花纹**）为底图，选区区域每帧 paste 回全亮
          底图 → **选区内部保持原亮度（明亮效果）**，四周平滑压暗，同微信/QQ。
          亮度刷新用 after 节流（边框原生跟手、亮度跟随，松手精确刷新）。
        * dark：整体半透明灰（canvas 深灰 + -alpha，旧版保留）。
    - 框选：左键拖选实时绘制；松手进入**调整态**（绿色选区 + 手柄）
    - 调整态：
        * 按住框内拖动 → 移动选区（方向键 1px / Shift+方向键 10px 微调）
        * 拖四角 / 四边手柄 → 缩放（对侧边/对角保持不动）
        * 拖顶部圆点手柄 → 绕中心旋转（Shift 吸附 15°，实时显示角度）
    - 确认：Enter / 双击框内 / 确认按钮 → 返回 CaptureResult
    - 取消：Esc / 右键；重选按钮 / 在选区外重新拖选
    - 截图：angle≈0 走轴对齐；否则按旋转选区的外接矩形从全屏背景裁剪，再用
      仿射**反旋转摆正**并精确取样，OCR 识别「摆正」后的内容（倾斜文字更准）
    - 异形框（操作条勾选「异形框」后）：选区变为**任意四边形**，4 个角点可
      自由拖动（不再保持矩形），顶边（旋转圆点所在边）标识为输出正上方；
      确认后按**透视矫正（四点映射）**把四边形映射为正矩形再做识别，
      适合透视倾斜/梯形变形的文字区域
    - 坐标：全部用 canvas 本地坐标，转屏幕坐标时加 canvas 根偏移；配合进程
      DPI 感知（cores/win32），保证与 ImageGrab 物理像素一致
"""
from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk

MIN_SIZE = 32  # 有效选区最小边长（px）
_MIN_QUAD_AREA = 256.0  # 异形框最小面积（px²，≈16×16），防止退化/自交四边形

_HINT_IDLE = "Выделите область для распознавания (можно повторно), Esc — отмена"
_HINT_SELECTED = ("Выбрано {w}×{h}: тяните внутри — переместить, углы/стороны — размер, "
                  "кружок — поворот; Enter/двойной клик/«Готово» — подтвердить, Esc — отмена")
_VEIL_BG = "#1e1e1e"        # dark 风格遮罩底色（经 -alpha 呈「单纯半透明灰」）
_COLOR_SELECT = "#00c8ff"   # 拖选高亮
_COLOR_CONFIRM = "#2ee06b"  # 调整态高亮
_COLOR_HANDLE = "#ffffff"   # 缩放手柄填充
_COLOR_ROTATE = "#ffb020"   # 旋转手柄
_DIM_RGBA = (16, 16, 16, 160)  # bright 旋转选区：选区外压暗色（半透明黑）

_HIT_RADIUS = 8      # 缩放手柄命中半径（px）
_ROT_HIT_RADIUS = 12 # 旋转手柄命中半径（px）
_HANDLE_HALF = 3     # 缩放手柄半边长（px）
_QUAD_HANDLE_HALF = 4  # 异形框角点手柄半边长（px）
_ROT_OFFSET = 18     # 旋转手柄距顶边距离（px）
_MOVE_THRESHOLD = 3  # 移动判定阈值（px，避免单击误移动）
_ANGLE_SNAP = 15     # Shift 吸附角度（度）
_ANGLE_TOL = 0.5     # 视为「不旋转」的角度阈值（度）

# 缩放手柄在选区局部坐标 (sx·hw, sy·hh) 中的位置定义。
# 局部轴：xaxis=R(θ)·(1,0)，yaxis=R(θ)·(0,1)（屏幕 y 向下 → 局部 +y 为视觉下方）。
_LOCAL_HANDLES = (
    ("tr", (1, -1)), ("tl", (-1, -1)), ("bl", (-1, 1)), ("br", (1, 1)),
    ("top", (0, -1)), ("left", (-1, 0)), ("bottom", (0, 1)), ("right", (1, 0)),
)


@dataclass
class CaptureResult:
    """一次确认后的选区结果（屏幕物理坐标）。

    (cx, cy) 为选区中心，w/h 为未旋转尺寸，angle 为旋转角度（度，≈0 表示未旋转）。
    quad 非 None 时表示「异形框」模式：4 个角点（屏幕坐标），顺序
    [顶左, 顶右, 底右, 底左]，其中 quad[0]→quad[1] 为顶边（旋转点所在边），
    确认后映射为输出矩形的上边。
    """
    cx: int
    cy: int
    w: int
    h: int
    angle: float = 0.0
    quad: list[tuple[float, float]] | None = None


# ---------------------------------------------------------------------------
# 纯几何工具（可单测，不依赖屏幕/窗口）
# ---------------------------------------------------------------------------


def rotated_rect_corners(cx: float, cy: float, w: float, h: float,
                         angle_deg: float) -> list[tuple[float, float]]:
    """绕中心逆时针旋转 angle_deg 度的矩形四角点（世界坐标）。"""
    a = math.radians(angle_deg)
    cos, sin = math.cos(a), math.sin(a)
    hw, hh = w / 2, h / 2
    return [(cx + dx * cos - dy * sin, cy + dx * sin + dy * cos)
            for dx, dy in ((hw, hh), (-hw, hh), (-hw, -hh), (hw, -hh))]


def outer_bbox(corners) -> tuple[int, int, int, int]:
    """角点列表 → 轴对齐外接矩形 (x0, y0, x1, y1)（向外取整）。"""
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (int(math.floor(min(xs))), int(math.floor(min(ys))),
            int(math.ceil(max(xs))), int(math.ceil(max(ys))))


def deskew_region(src: Image.Image, xmin: float, ymin: float,
                  cx: float, cy: float, w: float, h: float,
                  angle_deg: float) -> Image.Image:
    """把含旋转选区的截图**反旋转摆正**并精确取样，返回选区内内容的正矩形图。

    src: 轴对齐外接矩形截图（由全屏背景裁剪而来，原点在屏幕 (xmin, ymin)）。
    输出尺寸 = 选区未旋转尺寸 (w, h)；通过仿射把「旋转选区内」的像素映射到
    输出像素，从而裁掉外接矩形角落的背景。
    """
    a = math.radians(angle_deg)
    cos, sin = math.cos(a), math.sin(a)
    hw, hh = w / 2, h / 2
    # PIL AFFINE：输出 (u,v) ← 输入 (a*u+b*v+c, d*u+e*v+f)
    # 输出 (u,v) → 世界 = center + R(θ)·(u-hw, v-hh) → 输入像素 = 世界 - (xmin,ymin)
    coeffs = (cos, -sin, cx - xmin - cos * hw + sin * hh,
              sin, cos, cy - ymin - sin * hw - cos * hh)
    return src.transform((max(1, round(w)), max(1, round(h))),
                         Image.AFFINE, coeffs, resample=Image.BICUBIC)


def warp_quad(img: np.ndarray, quad) -> np.ndarray:
    """把任意四边形**透视矫正**为矩形（BGR/RGB ndarray 均可，不关心通道序）。

    quad: 4 个点（须落在 img 像素坐标内），顺序 [顶左, 顶右, 底右, 底左]：
        - 顶边 quad[0]→quad[1] 映射为输出上边（异形框模式下为旋转点所在边）
        - 右边 quad[1]→quad[2] 映射为输出右边
        - 底边 quad[2]→quad[3] 映射为输出下边
        - 左边 quad[3]→quad[0] 映射为输出左边
    输出尺寸：宽 = max(顶边长, 底边长)，高 = max(左边长, 右边长)（文档矫正惯例，
    近似保持原比例）。退化四边形（近共线/自交）由调用方保证不出现。
    """
    import cv2

    src = np.asarray(quad, dtype="float32")
    tl, tr, br, bl = src[0], src[1], src[2], src[3]
    w = max(MIN_SIZE, int(round(max(_dist(*tl, *tr), _dist(*bl, *br)))))
    h = max(MIN_SIZE, int(round(max(_dist(*tl, *bl), _dist(*tr, *br)))))
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                   dtype="float32")
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, matrix, (w, h),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)


def _polygon_area(pts) -> float:
    """多边形（4 点）鞋带公式面积（绝对值）。近退化/自交时接近 0。"""
    s = 0.0
    for i in range(4):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % 4]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _screen_size() -> tuple[int, int]:
    """当前主屏物理分辨率（clamp 截图 bbox 用）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    except Exception:  # pragma: no cover - 非 Windows/异常兜底
        return 1920, 1080


# ---------------------------------------------------------------------------
# 截图：先全屏背景 → 遮罩框选 → 确认后裁选区（Spec §4.1 触发项）
# ---------------------------------------------------------------------------


def capture_background() -> Image.Image:
    """截取整屏背景（含所有窗口、任务栏），作为遮罩底图与选区裁图源。

    触发截图的第一步：**先截图再弹遮罩**（微信/QQ 截图做法），这样确认后裁的
    是热键按下那一刻的真实屏幕，遮罩本身不会被截进去，也不依赖各窗口对
    Tk 遮罩的透明叠加。返回 RGB PIL Image（物理像素尺寸）。
    """
    sw, sh = _screen_size()
    return ImageGrab.grab(bbox=(0, 0, sw, sh)).convert("RGB")


def grab_screen(bbox: tuple[int, int, int, int]) -> np.ndarray:
    """按屏幕物理坐标 (x, y, w, h) 截图，返回 BGR ndarray（OCR 输入格式）。

    保留的独立入口（全屏背景失败时的回退路径由 App 决定）；一般场景直接用
    extract_region() 从已截好的全屏背景裁选区。
    """
    x, y, w, h = bbox
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    return np.array(img.convert("RGB"))[:, :, ::-1]  # RGB → BGR


def _to_bgr(img: Image.Image) -> np.ndarray:
    return np.array(img)[:, :, ::-1]  # RGB → BGR


def _extract_quad(background: Image.Image, quad, sw: int, sh: int) -> np.ndarray:
    """异形框：按四边形外接矩形裁剪全屏背景 → 透视矫正为矩形，返回 BGR。

    顶边 quad[0]→quad[1]（旋转点所在边）映射为输出矩形上边。
    """
    x0, y0, x1, y1 = outer_bbox(quad)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(sw, x1), min(sh, y1)
    if x1 - x0 < MIN_SIZE or y1 - y0 < MIN_SIZE:   # 退化：至少保留 MIN_SIZE
        x1 = max(x1, x0 + MIN_SIZE)
        y1 = max(y1, y0 + MIN_SIZE)
    src = background.crop((x0, y0, x1, y1))
    img_bgr = _to_bgr(src)
    local_quad = [(x - x0, y - y0) for x, y in quad]
    return warp_quad(img_bgr, local_quad)


def extract_region(background: Image.Image, result: CaptureResult) -> np.ndarray:
    """从全屏背景裁出选区内容，返回 BGR ndarray。

    异形框（result.quad 非 None）走透视矫正；angle≈0 走轴对齐裁剪；否则按
    旋转选区的外接矩形裁剪后反旋转摆正（返回「摆正」后的内容图）。原点即
    背景图左上角（屏幕 (0,0)），因此坐标直接按物理像素换算，无偏移。
    """
    sw, sh = background.size
    cx, cy, w, h, angle = result.cx, result.cy, result.w, result.h, result.angle
    if result.quad is not None:
        return _extract_quad(background, result.quad, sw, sh)
    if abs(angle) < _ANGLE_TOL:
        x = max(0, int(round(cx - w / 2)))
        y = max(0, int(round(cy - h / 2)))
        right = min(sw, x + max(MIN_SIZE, w))
        bottom = min(sh, y + max(MIN_SIZE, h))
        x, y = min(x, right - MIN_SIZE), min(y, bottom - MIN_SIZE)
        return _to_bgr(background.crop((x, y, right, bottom)))
    corners = rotated_rect_corners(cx, cy, w, h, angle)
    x0, y0, x1, y1 = outer_bbox(corners)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(sw, x1), min(sh, y1)
    src = background.crop((x0, y0, x1, y1))
    upright = deskew_region(src, x0, y0, cx, cy, w, h, angle)
    return _to_bgr(upright)


# ---------------------------------------------------------------------------
# 框选遮罩
# ---------------------------------------------------------------------------


def _dist(x0, y0, x1, y1) -> float:
    return math.hypot(x1 - x0, y1 - y0)


class CaptureOverlay:
    """模态框选遮罩（支持移动 / 缩放 / 旋转）。

    底图：默认以 `capture_background()` 截取的全屏图为底（bright 风格），
    也可传 background=None 走 dark 风格（纯半透明灰，旧版行为，供测试/回退）。
    style="bright"：背景图上叠一层半透明黑遮罩，选区内部"挖空"恢复原亮度 →
    明亮选区 + 四周压暗（微信/QQ 效果）。
    style="dark"：整体半透明灰 + 透明选区（旧版）。

    状态机：
        idle ──拖选──▶ selected(调整态)──确认/Enter/双击──▶ 返回 CaptureResult
        selected ──拖手柄──▶ resize/rotate ──松开──▶ selected
        selected ──框内拖动──▶ move ──松开──▶ selected
        selected ──方向键──▶ selected（微调位置）
        selected ──框外拖选──▶ dragging ──(过小)──▶ 恢复原选区 / (有效)──▶ selected
        任意态 ──取消/Esc/右键──▶ 返回 None
    """

    def __init__(self, root: tk.Tk, opacity: float = 0.4,
                 background: Image.Image | None = None,
                 style: str = "bright") -> None:
        self._root = root
        self._result: CaptureResult | None = None
        self._sel: tuple[float, float, float, float, float] | None = None  # (cx,cy,w,h,angle)
        self._prev_sel = None                 # 框外重选前的原选区（过小时恢复）
        self._had_selection = False
        self._state = "idle"                  # idle|dragging|selected|move*|resize:*|rotating
        self._start: tuple[int, int] | None = None    # 新建框选的按下点
        self._press: tuple[int, int] | None = None    # 调整态按下点
        self._sel0: tuple | None = None       # 调整开始时的选区快照
        self._start_angle = 0.0               # 旋转按下时光标相对中心的角度
        self._sel_angle0 = 0.0                # 旋转按下时选区角度
        self._rect_id: int | None = None
        self._hint_id: int | None = None
        self._action_id: int | None = None
        self._hover: str | None = None             # 当前悬停的手柄类型 (rot/tr/tl/bl/br/top/bottom/left/right 或 None)
        self._cursor_now: str | None = None         # 当前设置的光标，避免重复 set
        self._rot_cursor: str = "exchange"          # 旋转光标（Windows 常见的循环箭头），必要时可改为自定义 .cur 路径
        # -- 异形框模式（操作条勾选「异形框」后启用）--------------------
        self._quad_mode = False                     # 是否处于异形框模式
        self._quad_var: tk.BooleanVar | None = None  # 勾选框状态（跨操作条重建保留）
        self._quad: list[tuple[float, float]] | None = None   # 4 角点 [顶左,顶右,底右,底左]（canvas 坐标）
        self._quad0: list[tuple[float, float]] | None = None  # 调整开始时的角点快照

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        self._sw, self._sh = sw, sh

        # 遮罩风格：bright 需要全屏截图底图（未提供时尝试现截，失败回退 dark）
        style = "bright" if style == "bright" else "dark"
        if style == "bright" and background is None:
            try:
                background = capture_background()
            except Exception:  # noqa: BLE001 - 截图失败回退旧式遮罩
                background = None
        if style == "bright" and background is not None:
            if background.size != (sw, sh):
                # 多屏/DPI 情形下背景图尺寸与窗口尺寸不一致时，仍可缩放到窗口尺寸
                # （能截全时尺寸一致，缩放恒为 1，无损失）
                background = background.resize((sw, sh), Image.BILINEAR)
        self._style = style
        self._background = background

        win = tk.Toplevel(root)
        win.overrideredirect(True)                 # 无边框
        win.attributes("-topmost", True)           # 置顶
        win.attributes("-alpha", 1.0)              # 明亮风格自身不透明（dark 用整体 -alpha）
        win.geometry(f"{sw}x{sh}+0+0")
        self._win = win

        canvas = tk.Canvas(win, cursor="cross", highlightthickness=0,
                           bg=_VEIL_BG)
        canvas.pack(fill="both", expand=True)
        self._canvas = canvas

        # bright 风格：预渲染「整屏压暗版」为底图，选区区域每帧 paste 回全亮
        # 底图 → 选区内部明亮、四周平滑半透明灰压暗（无 stipple 花纹）。
        self._backdrop_id: int | None = None     # 压暗版底图 PhotoImage
        self._dim_base: Image.Image | None = None   # 预渲染的压暗版（RGB PIL）
        self._photo: tk.PhotoImage | None = None    # 当前显示图（防 GC）
        self._bright_after_id: str | None = None    # 亮度节流 after 句柄
        if style == "bright" and background is not None:
            canvas.configure(bg="black")
            # 预渲染整屏压暗版（一次性，~90ms）：numpy 亮度缩放 → 平滑半透明灰
            dim_arr = np.clip(
                np.asarray(background, dtype=np.int16) * self._dim_factor,
                0, 255).astype(np.uint8)
            self._dim_base = Image.fromarray(dim_arr)
            self._photo = ImageTk.PhotoImage(image=self._dim_base)
            self._backdrop_id = canvas.create_image(0, 0, anchor="nw",
                                                    image=self._photo)
            # 全亮底图仅作 paste 取源（_make_bright_image 用 self._background）
            self._bright_id: int | None = None       # 明亮洞 overlay image item id
            self._bright_photo: tk.PhotoImage | None = None  # 防止 GC
        else:
            win.attributes("-alpha", max(0.15, min(0.7, float(opacity))))

        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<Double-Button-1>", self._on_double_click)  # 双击确认
        canvas.bind("<Button-3>", lambda _e: self._cancel())     # 右键取消
        canvas.bind("<Motion>", self._on_motion)                 # 鼠标悬停 → 动态光标/手柄高亮
        win.bind("<Escape>", lambda _e: self._cancel())
        win.bind("<KeyPress-Return>", lambda _e: self._confirm())
        for ks in ("<Left>", "<Right>", "<Up>", "<Down>"):
            win.bind(ks, self._on_arrow)
            canvas.bind(ks, self._on_arrow)

        self._set_hint(_HINT_IDLE)
        win.lift()
        win.focus_force()
        win.attributes("-topmost", True)

    # -- 公开接口 ---------------------------------------------------------

    def select(self) -> CaptureResult | None:
        """模态运行：确认返回 CaptureResult；取消返回 None。"""
        self._win.wait_window()
        return self._result

    def destroy(self) -> None:
        if self._win.winfo_exists():
            self._win.destroy()

    # -- 提示 -------------------------------------------------------------

    def _set_hint(self, text: str) -> None:
        c = self._canvas
        if self._hint_id is not None:
            c.delete(self._hint_id)
        self._hint_id = c.create_text(
            self._sw // 2, 24, text=text, fill="white",
            font=("Segoe UI", 12), width=self._sw - 40, justify="center",
        )

    def _flash_hint(self, text: str) -> None:
        c = self._canvas
        c.delete("flash")
        c.create_text(
            self._sw // 2, self._sh // 2, text=text, fill="#ff5a5a",
            font=("Segoe UI", 14, "bold"), tags="flash",
            width=self._sw - 40, justify="center",
        )

        def _clear():
            if self._win.winfo_exists():
                c.delete("flash")
                if self._state != "selected":
                    self._set_hint(_HINT_IDLE)

        self._root.after(1500, _clear)

    # -- 操作条（确认 / 重选 / 取消） --------------------------------------

    def _show_action_bar(self) -> None:
        if self._action_id is not None or self._sel is None:
            return
        bar = ttk.Frame(self._win)
        if self._quad_var is None:
            self._quad_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Четырёхугольник", variable=self._quad_var,
                        command=self._on_quad_toggle).pack(side="left", padx=2)
        ttk.Button(bar, text="Готово", width=8, command=self._confirm).pack(side="left", padx=2)
        ttk.Button(bar, text="Заново", width=8, command=self._reselect).pack(side="left", padx=2)
        ttk.Button(bar, text="Отмена", width=8, command=self._cancel).pack(side="left", padx=2)
        cx, cy, w, h, angle = self._sel
        _x0, _y0, x1, y1 = outer_bbox(rotated_rect_corners(cx, cy, w, h, angle))
        by = y1 + 14
        if by + 44 > self._sh:
            by = max(y1 - 48, 4)   # 底部放不下则放选区上方
        self._action_id = self._canvas.create_window((x1 + cx) // 2, by, window=bar)

    def _on_quad_toggle(self) -> None:
        """操作条「异形框」勾选/取消：切换选区几何为自由四边形 / 矩形。"""
        if self._quad_var is None:
            return
        on = bool(self._quad_var.get())
        if on == self._quad_mode:
            return
        if on:
            self._enter_quad_mode()
        else:
            self._exit_quad_mode()
        if self._win.winfo_exists():
            self._win.focus_force()   # 恢复键盘焦点，方向键可微调

    def _enter_quad_mode(self) -> None:
        """开启异形框：把当前矩形选区转成 4 角点自由四边形。"""
        self._quad_mode = True
        self._quad0 = None
        if self._sel is not None:
            self._quad = self._rect_to_quad(self._sel)
            self._draw_selected()
            self._set_hint("Четырёхугольник: тяните 4 угла (верхняя сторона станет верхом), "
                           "кружок — поворот; Enter/двойной клик/«Готово» — подтвердить")
        else:
            self._set_hint("Режим четырёхугольника: выделите область, затем тяните 4 угла; Esc — отмена")

    def _exit_quad_mode(self) -> None:
        """关闭异形框：回到矩形调整态（以当前四边形外接矩形为矩形）。"""
        self._quad_mode = False
        self._quad = None
        self._quad0 = None
        if self._sel is not None:
            self._draw_selected()
            cx, cy, w, h, _angle = self._sel
            self._set_hint(_HINT_SELECTED.format(w=round(w), h=round(h)))

    def _hide_action_bar(self) -> None:
        if self._action_id is not None:
            self._canvas.delete(self._action_id)
            self._action_id = None

    # -- 几何辅助 ---------------------------------------------------------

    def _local_axes(self, angle: float) -> tuple[tuple[float, float], tuple[float, float]]:
        a = math.radians(angle)
        cos, sin = math.cos(a), math.sin(a)
        return (cos, sin), (-sin, cos)   # xaxis, yaxis

    def _rect_to_quad(self, sel) -> list[tuple[float, float]]:
        """矩形选区 → 异形框初始 4 角点 [顶左, 顶右, 底右, 底左]（顶边=旋转点边）。

        rotated_rect_corners 返回顺序为 [BR, BL, TL, TR]（angle=0 时），
        重排为顶边在前、顺时针。
        """
        cx, cy, w, h, angle = sel
        c = rotated_rect_corners(cx, cy, w, h, angle)
        return [c[2], c[3], c[0], c[1]]   # TL, TR, BR, BL

    def _current_corners(self):
        """当前选区的 4 角点：异形框用 quad；矩形用旋转矩形角点。"""
        if self._quad_mode and self._quad is not None:
            return self._quad
        if self._sel is None:
            return None
        cx, cy, w, h, angle = self._sel
        return rotated_rect_corners(cx, cy, w, h, angle)

    def _quad_center(self) -> tuple[float, float]:
        q = self._quad
        return (sum(p[0] for p in q) / 4.0, sum(p[1] for p in q) / 4.0)

    def _quad_bbox(self) -> tuple[float, float, float, float]:
        return outer_bbox(self._quad)

    def _quad_rot_pos(self) -> tuple[float, float]:
        """旋转圆点位置：顶边（quad[0]→quad[1]）中点沿外法线外移。"""
        q = self._quad
        mx = (q[0][0] + q[1][0]) / 2.0
        my = (q[0][1] + q[1][1]) / 2.0
        ex, ey = q[1][0] - q[0][0], q[1][1] - q[0][1]
        length = math.hypot(ex, ey)
        if length < 1e-6:
            nx, ny = 0.0, -1.0
        else:
            nx, ny = -ey / length, ex / length   # 顶边逆时针转90°（指向屏幕上方侧）
        cx, cy = self._quad_center()
        if nx * (mx - cx) + ny * (my - cy) < 0:
            nx, ny = -nx, -ny                    # 确保外法线指向远离中心一侧
        return mx + nx * _ROT_OFFSET, my + ny * _ROT_OFFSET

    def _rotate_quad_points(self, pts, cx: float, cy: float,
                            deg: float) -> list[tuple[float, float]]:
        a = math.radians(deg)
        cos, sin = math.cos(a), math.sin(a)
        return [(cx + (x - cx) * cos - (y - cy) * sin,
                 cy + (x - cx) * sin + (y - cy) * cos) for x, y in pts]

    def _sync_sel_from_quad(self) -> None:
        """异形框几何变化后，把 _sel 同步为外接矩形（供提示/夹紧/退出复用）。"""
        x0, y0, x1, y1 = self._quad_bbox()
        self._sel = ((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                     max(MIN_SIZE, x1 - x0), max(MIN_SIZE, y1 - y0), 0.0)

    def _point_in_quad(self, x: float, y: float) -> bool:
        """点在四边形内（射线法）。"""
        q = self._quad
        inside = False
        j = 3
        for i in range(4):
            xi, yi = q[i]
            xj, yj = q[j]
            if ((yi > y) != (yj > y)) and \
                    (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _handle_positions(self):
        """当前选区的手柄位置列表：[(kind, x, y), ...] + 旋转手柄 ("rot", x, y)。

        异形框模式：4 个角点手柄 q0..q3 + 顶边旋转圆点；矩形模式：8 手柄。
        """
        if self._quad_mode and self._quad is not None:
            handles = [(f"q{i}", p[0], p[1]) for i, p in enumerate(self._quad)]
            rx, ry = self._quad_rot_pos()
            return handles, ("rot", rx, ry)
        cx, cy, w, h, angle = self._sel
        hw, hh = w / 2, h / 2
        xaxis, yaxis = self._local_axes(angle)
        handles = []
        for kind, (sx, sy) in _LOCAL_HANDLES:
            hx = cx + sx * hw * xaxis[0] + sy * hh * yaxis[0]
            hy = cy + sx * hw * xaxis[1] + sy * hh * yaxis[1]
            handles.append((kind, hx, hy))
        rhx = cx - (hh + _ROT_OFFSET) * yaxis[0]
        rhy = cy - (hh + _ROT_OFFSET) * yaxis[1]
        return handles, ("rot", rhx, rhy)

    def _hit_handle(self, x: float, y: float):
        """命中手柄则返回 (kind, hx, hy)，否则 None；旋转手柄优先。"""
        if self._sel is None:
            return None
        handles, rot = self._handle_positions()
        if _dist(x, y, rot[1], rot[2]) <= _ROT_HIT_RADIUS:
            return rot
        for kind, hx, hy in handles:
            if _dist(x, y, hx, hy) <= _HIT_RADIUS:
                return (kind, hx, hy)
        return None

    def _point_in_rect(self, x: float, y: float) -> bool:
        if self._sel is None:
            return False
        if self._quad_mode and self._quad is not None:
            return self._point_in_quad(x, y)
        cx, cy, w, h, angle = self._sel
        xaxis, yaxis = self._local_axes(angle)
        dx, dy = x - cx, y - cy
        lx = dx * xaxis[0] + dy * xaxis[1]
        ly = dx * yaxis[0] + dy * yaxis[1]
        return abs(lx) <= w / 2 and abs(ly) <= h / 2

    # -- bright 风格：预渲染压暗版 + 选区明亮洞 -----------------------------
    # 目标效果（微信/QQ 截图）：选区内部保持原亮度（明亮）、选区外为**平滑
    # 半透明灰**压暗（无 stipple 花纹）。
    # 实现：
    #   - 打开遮罩时一次性预渲染整屏「压暗版」`_dim_base`（numpy 亮度缩放，
    #     平滑半透明灰；~90ms 只在打开时一次），贴为底图 `_backdrop_id`
    #   - 每帧拖选/调整：从压暗版 `copy()`，把选区区域用全亮底图
    #     `Image.paste` 回来（明亮洞）→ 只处理选区区域（几 ms，流畅）
    #   - 旋转选区：挖空为角点多边形（低频，代价可接受）
    # 关键：Tk PhotoImage 不支持 alpha（实测 RGBA 被误读成白色/青色），所以
    # 半透明灰必须在 Python 侧用 PIL 合成好，再贴为不透明 PhotoImage。

    _dim_factor: float = 0.55   # 压暗系数：55% 亮度（平滑半透明灰，无花纹）

    def _make_bright_image(self) -> Image.Image | None:
        """构造「压暗版 + 选区明亮洞」的最终 RGB 图。

        无选区 → 整屏压暗；有选区 → 选区区域从全亮底图贴回（明亮）。
        旋转选区按角点多边形挖洞。
        """
        base = self._dim_base
        if base is None or self._background is None:
            return None
        out = base.copy()
        if self._sel is None:
            return out
        cx, cy, w, h, angle = self._sel
        if abs(angle) < _ANGLE_TOL and not self._quad_mode:
            left = max(0, int(round(cx - w / 2)))
            top = max(0, int(round(cy - h / 2)))
            right = min(base.width, int(round(cx + w / 2)))
            bottom = min(base.height, int(round(cy + h / 2)))
            if right > left and bottom > top:
                region = self._background.crop((left, top, right, bottom))
                out.paste(region, (left, top))
        else:
            # 旋转/异形框：洞为角点多边形 → 用 mask 挖回全亮区域
            corners = self._current_corners()
            mask = Image.new("L", base.size, 0)
            ImageDraw.Draw(mask).polygon(
                [tuple(int(round(p)) for p in pt) for pt in corners], fill=255)
            out.paste(self._background, (0, 0), mask)
        return out

    def _make_bright_region(self) -> tuple[tuple[int, int, int, int], Image.Image] | None:
        """仅合成选区外接矩形所在的小区域图，返回 (左,上,右,下, 区域图)。

        拖动/调整时只需刷新这个 bbox 范围，避免每帧重建整屏 PhotoImage。
        无选区时返回整屏范围（等价于「全屏压暗」）。
        """
        if self._dim_base is None or self._background is None:
            return None
        if self._sel is None:
            return (0, 0, self._dim_base.width, self._dim_base.height), self._dim_base.copy()
        cx, cy, w, h, angle = self._sel
        if abs(angle) < _ANGLE_TOL and not self._quad_mode:
            left = max(0, int(round(cx - w / 2)))
            top = max(0, int(round(cy - h / 2)))
            right = min(self._dim_base.width, int(round(cx + w / 2)))
            bottom = min(self._dim_base.height, int(round(cy + h / 2)))
            if right <= left or bottom <= top:
                return None
            region_dim = self._dim_base.crop((left, top, right, bottom))
            bright_src = self._background.crop((left, top, right, bottom))
            region_dim.paste(bright_src, (0, 0))
            return (left, top, right, bottom), region_dim
        # 旋转/异形框：洞为角点多边形 → 仅在外接 bbox 上做 mask 合成
        corners = self._current_corners()
        xs = [int(round(p[0])) for p in corners]
        ys = [int(round(p[1])) for p in corners]
        left = max(0, min(xs))
        top = max(0, min(ys))
        right = min(self._dim_base.width, max(xs))
        bottom = min(self._dim_base.height, max(ys))
        if right <= left or bottom <= top:
            return None
        region_dim = self._dim_base.crop((left, top, right, bottom))
        region_bright = self._background.crop((left, top, right, bottom))
        mask = Image.new("L", (right - left, bottom - top), 0)
        local_corners = [(x - left, y - top) for x, y in zip(xs, ys)]
        ImageDraw.Draw(mask).polygon(local_corners, fill=255)
        region_dim.paste(region_bright, (0, 0), mask)
        return (left, top, right, bottom), region_dim

    def _redraw_bright(self) -> None:
        """根据当前选区，在幕布上叠加「明亮洞」小区域图。

        _backdrop_id 始终显示全屏压暗底图（init 时创建、后续不再变更），
        _bright_id 为叠加在选区位置的亮孔 overlay（有选区时显示，无选区时删除）。
        只重建选区 bbox 的小区域 PhotoImage，大幅降低每帧开销。
        """
        c = self._canvas
        if not c.winfo_exists():
            return
        region = self._make_bright_region()
        if region is None:
            # 没有有效区域：删除 overlay，显示全屏压暗
            if self._bright_id is not None and c.winfo_exists():
                c.delete(self._bright_id)
                self._bright_id = None
                self._bright_photo = None
            return
        (left, top, _right, _bottom), img = region
        photo = ImageTk.PhotoImage(image=img)
        self._bright_photo = photo               # 防 GC (overlay)
        if self._bright_id is None:
            # 首次创建 overlay，位于幕布之上、选区 UI 之下
            self._bright_id = c.create_image(left, top, anchor="nw",
                                             image=photo, tags="bright")
        else:
            c.itemconfig(self._bright_id, image=photo)
            c.coords(self._bright_id, left, top)
        # 确保选区 UI（sel/handle/size/angle）始终在 overlay 之上
        c.tag_raise("sel")
        c.tag_raise("handle")
        c.tag_raise("size")
        c.tag_raise("angle")

    def _show_full_dim(self) -> None:
        """无选区：整屏压暗（idle/重选时）。

        删除明亮洞 overlay，显示全屏压暗底图（init 时已创建的 _backdrop_id）。
        """
        c = self._canvas
        if self._bright_id is not None and c.winfo_exists():
            c.delete(self._bright_id)
            self._bright_id = None
            self._bright_photo = None

    # -- 绘制 -------------------------------------------------------------

    def _clear_selected_items(self) -> None:
        c = self._canvas
        c.delete("sel")
        c.delete("handle")
        c.delete("size")
        c.delete("angle")
        self._rect_id = None

    # 亮度刷新节流（ms）：拖动/调整时亮度用 after 节流更新，边框始终原生跟手
    _BRIGHT_THROTTLE_MS = 16

    def _schedule_bright_refresh(self, force: bool = False) -> None:
        """节流调度亮度重建：拖动中不每帧重建（边框先画、原生流畅）。

        force=True（松手/关键状态）立即重建；否则合并到下一次 after 回调。
        """
        if self._style != "bright" or self._background is None:
            return
        if force:
            if getattr(self, "_bright_after_id", None) is not None:
                try:
                    self._root.after_cancel(self._bright_after_id)
                except Exception:  # noqa: BLE001 - 已触发则忽略
                    pass
                self._bright_after_id = None
            self._redraw_bright()
            return
        if getattr(self, "_bright_after_id", None) is None:
            def _flush():
                self._bright_after_id = None
                if self._win.winfo_exists():
                    self._redraw_bright()
            self._bright_after_id = self._root.after(self._BRIGHT_THROTTLE_MS,
                                                     _flush)

    def _draw_selection_ui(self) -> None:
        """绘制/更新选区边框 + 手柄 + 尺寸（原生 canvas，拖动跟手）。"""
        c = self._canvas
        if self._sel is None:
            return
        if self._quad_mode and self._quad is not None:
            self._draw_quad_ui()
            return
        cx, cy, w, h, angle = self._sel
        hw, hh = w / 2, h / 2
        corners = rotated_rect_corners(cx, cy, w, h, angle)
        pts = [v for p in corners for v in p]
        if self._rect_id is None:
            self._rect_id = c.create_polygon(*pts, outline=_COLOR_CONFIRM,
                                             width=2, fill="", tags="sel")
        else:
            c.coords(self._rect_id, *pts)
        handles, (_rot_kind, rhx, rhy) = self._handle_positions()
        # 手柄：位置随选区变化 → 重建（数量少，开销可忽略）
        c.delete("handle")
        for _kind, hx, hy in handles:
            hov = (_kind == self._hover)
            c.create_rectangle(hx - _HANDLE_HALF, hy - _HANDLE_HALF,
                               hx + _HANDLE_HALF, hy + _HANDLE_HALF,
                               fill=_COLOR_HANDLE,
                               outline="#ffd24d" if hov else _COLOR_CONFIRM,
                               width=2 if hov else 1, tags="handle")
        rot_hov = (self._hover == "rot")
        r = 6 if rot_hov else 5
        c.create_oval(rhx - r, rhy - r, rhx + r, rhy + r,
                      fill=_COLOR_ROTATE, outline="#ffd24d" if rot_hov else _COLOR_ROTATE,
                      width=2 if rot_hov else 1, tags="handle")
        xaxis, yaxis = self._local_axes(angle)
        c.delete("size")
        # 尺寸标签：顶边（局部 -y）上方
        tx = cx - hh * yaxis[0] - 20 * yaxis[0]
        ty = cy - hh * yaxis[1] - 20 * yaxis[1]
        c.create_text(tx, ty, text=f"{w:.0f} × {h:.0f}", fill="white",
                      font=("Segoe UI", 10), tags="size")
        c.delete("angle")
        if abs(angle) >= _ANGLE_TOL:
            c.create_text(rhx, rhy - 16, text=f"{angle:.0f}°", fill="white",
                          font=("Segoe UI", 10), tags="angle")

    def _draw_quad_ui(self) -> None:
        """异形框：4 个角点手柄 + 顶边高亮线 + 顶边旋转圆点 + 外接尺寸。"""
        c = self._canvas
        q = self._quad
        pts = [v for p in q for v in p]
        if self._rect_id is None:
            self._rect_id = c.create_polygon(*pts, outline=_COLOR_CONFIRM,
                                             width=2, fill="", tags="sel")
        else:
            c.coords(self._rect_id, *pts)
        # 角点手柄（q0..q3）
        c.delete("handle")
        for i, (hx, hy) in enumerate(q):
            hov = (self._hover == f"q{i}")
            c.create_rectangle(hx - _QUAD_HANDLE_HALF, hy - _QUAD_HANDLE_HALF,
                               hx + _QUAD_HANDLE_HALF, hy + _QUAD_HANDLE_HALF,
                               fill=_COLOR_HANDLE,
                               outline="#ffd24d" if hov else _COLOR_CONFIRM,
                               width=2 if hov else 1, tags="handle")
        # 顶边（quad[0]→quad[1]）高亮：标识将作为输出矩形正上方
        c.create_line(q[0][0], q[0][1], q[1][0], q[1][1],
                      fill=_COLOR_ROTATE, width=3, tags="handle")
        # 顶边中点旋转圆点
        rhx, rhy = self._quad_rot_pos()
        rot_hov = (self._hover == "rot")
        r = 6 if rot_hov else 5
        c.create_oval(rhx - r, rhy - r, rhx + r, rhy + r,
                      fill=_COLOR_ROTATE,
                      outline="#ffd24d" if rot_hov else _COLOR_ROTATE,
                      width=2 if rot_hov else 1, tags="handle")
        # 外接尺寸标签
        x0, y0, x1, y1 = outer_bbox(q)
        c.delete("size")
        c.create_text((x0 + x1) / 2, max(0, y0 - 16),
                      text=f"Четырёхугольник {x1 - x0:.0f}×{y1 - y0:.0f}", fill="white",
                      font=("Segoe UI", 10), tags="size")
        c.delete("angle")

    def _draw_selected(self) -> None:
        """调整态绘制：先画原生边框（跟手），亮度节流重建（不阻塞拖动）。"""
        if self._sel is None or not self._canvas.winfo_exists():
            return
        c = self._canvas
        if self._style == "bright" and self._background is not None:
            self._schedule_bright_refresh()   # 节流亮度（拖动不卡边框）
        self._clear_selected_items()
        self._draw_selection_ui()

    # -- 鼠标交互 ---------------------------------------------------------

    def _start_new_selection(self, event: tk.Event) -> None:
        """在选区外/无选区按下：开始新一次框选。"""
        self._hide_action_bar()
        self._prev_sel = self._sel if self._state == "selected" else None
        self._had_selection = self._prev_sel is not None
        self._start = (event.x, event.y)
        self._press = (event.x, event.y)
        self._state = "dragging"
        self._quad = None      # 新框选从矩形开始，进入调整态后按需转四边形
        self._quad0 = None
        self._hover = None               # 重置 hover 状态
        self._set_cursor("crosshair")    # 确保拖选时光标正确
        self._clear_selected_items()

    def _on_press(self, event: tk.Event) -> None:
        if self._state == "dragging":
            return
        if self._state == "selected" and self._sel is not None:
            h = self._hit_handle(event.x, event.y)
            if h is not None:
                self._hide_action_bar()  # 调整期间隐藏操作条，松开后在新位置重绘
                kind, _hx, _hy = h
                self._press = (event.x, event.y)
                if kind == "rot":
                    if self._quad_mode and self._quad is not None:
                        cx, cy = self._quad_center()
                        self._start_angle = math.atan2(event.y - cy, event.x - cx)
                        self._quad0 = [list(p) for p in self._quad]
                    else:
                        self._start_angle = math.atan2(
                            event.y - self._sel[1], event.x - self._sel[0])
                        self._sel_angle0 = self._sel[4]
                    self._state = "rotating"
                elif kind in ("q0", "q1", "q2", "q3"):
                    self._quad0 = [list(p) for p in self._quad]
                    self._state = f"quad:{kind}"
                else:
                    self._sel0 = self._sel
                    self._state = f"resize:{kind}"
                return
            if self._point_in_rect(event.x, event.y):
                self._hide_action_bar()
                self._press = (event.x, event.y)
                if self._quad_mode and self._quad is not None:
                    self._quad0 = [list(p) for p in self._quad]
                else:
                    self._sel0 = self._sel
                self._state = "move-pending"
                return
            self._start_new_selection(event)
            return
        self._start_new_selection(event)

    def _on_drag(self, event: tk.Event) -> None:
        if self._state == "dragging" and self._start is not None:
            self._drag_create(event)
            return
        if self._state == "move-pending":
            if _dist(event.x, event.y, self._press[0], self._press[1]) > _MOVE_THRESHOLD:
                self._state = "moving"
                self._move_to(event)
                self._draw_selected()
            return
        if self._state == "moving":
            self._move_to(event)
            self._draw_selected()
            return
        if self._state.startswith("resize:"):
            kind = self._state.split(":", 1)[1]
            self._apply_resize(kind, event)
            self._draw_selected()
            return
        if self._state.startswith("quad:"):
            kind = self._state.split(":", 1)[1]
            self._apply_quad_drag(kind, event)
            self._draw_selected()
            return
        if self._state == "rotating":
            self._rotate_to(event, shift=bool(event.state & 0x0001))
            self._draw_selected()

    def _on_motion(self, event: tk.Event) -> None:
        """鼠标悬停：动态更新光标 + 手柄高亮。"""
        if not self._win.winfo_exists():
            return
        cursor = self._cursor_for(event.x, event.y)
        self._set_cursor(cursor)
        # 仅在 selected 态更新 hover 状态并高亮对应手柄
        if self._state == "selected" and self._sel is not None:
            h = self._hover_handle(event.x, event.y)
            if h != self._hover:
                self._hover = h
                self._draw_selection_ui()   # 仅重绘手柄（含高亮）和角度文字
        elif self._hover is not None:
            self._hover = None
            self._draw_selection_ui()

    def _cursor_for(self, x: int, y: int) -> str:
        """根据鼠标位置和当前状态返回应显示的光标名。"""
        if self._state == "dragging":
            return "crosshair"
        if self._state in ("move-pending", "moving"):
            return "fleur"
        if self._state.startswith("resize:"):
            kind = self._state.split(":", 1)[1]
            return self._cursor_for_kind(kind)
        if self._state.startswith("quad:"):
            kind = self._state.split(":", 1)[1]
            return self._quad_cursor(kind)
        if self._state == "rotating":
            return self._rot_cursor
        if self._state != "selected" or self._sel is None:
            return "crosshair"
        handles, rot = self._handle_positions()
        if _dist(x, y, rot[1], rot[2]) <= _ROT_HIT_RADIUS:
            return self._rot_cursor
        for kind, hx, hy in handles:
            if _dist(x, y, hx, hy) <= _HIT_RADIUS:
                if self._quad_mode:
                    return self._quad_cursor(kind)
                return self._cursor_for_kind(kind)
        if self._point_in_rect(x, y):
            return "fleur"
        return "crosshair"

    def _quad_cursor(self, kind: str) -> str:
        """异形框角点光标：按角点编号取四向箭头（外观提示）。"""
        mapping = {"q0": "top_left_corner", "q1": "top_right_corner",
                   "q2": "bottom_right_corner", "q3": "bottom_left_corner"}
        return mapping.get(kind, "fleur")

    def _cursor_for_kind(self, kind: str) -> str:
        """返回对应手柄类型的光标名。"""
        if kind == "rot":
            return self._rot_cursor
        cx, cy, w, h, angle = self._sel
        xaxis, yaxis = self._local_axes(angle)
        if kind in ("top", "bottom"):
            # 局部 y 轴投影到屏幕：竖直程度决定竖向/横向光标
            return "sb_v_double_arrow" if abs(yaxis[1]) >= abs(yaxis[0]) \
                else "sb_h_double_arrow"
        if kind in ("left", "right"):
            return "sb_h_double_arrow" if abs(xaxis[0]) >= abs(xaxis[1]) \
                else "sb_v_double_arrow"
        # 四角：按角在屏幕上的主方向选对角光标
        hw, hh = w / 2, h / 2
        sx = 1 if kind in ("tr", "br") else -1
        sy = 1 if kind in ("bl", "br") else -1
        vx = sx * hw * xaxis[0] + sy * hh * yaxis[0]
        vy = sx * hw * xaxis[1] + sy * hh * yaxis[1]
        if vx >= 0 and vy <= 0:
            return "top_right_corner"
        if vx <= 0 and vy <= 0:
            return "top_left_corner"
        if vx <= 0 and vy >= 0:
            return "bottom_left_corner"
        return "bottom_right_corner"

    def _set_cursor(self, cursor: str) -> None:
        """设置 canvas 光标，避免无意义重复 configure。"""
        if cursor == self._cursor_now:
            return
        try:
            self._canvas.configure(cursor=cursor)
            self._cursor_now = cursor
        except tk.TclError:
            # 不支持的光标（如自定义 .cur）回退到 crosshair
            fallback = "crosshair" if cursor.startswith("@") else "arrow"
            try:
                self._canvas.configure(cursor=fallback)
                self._cursor_now = fallback
            except tk.TclError:
                pass

    def _hover_handle(self, x: float, y: float) -> str | None:
        """返回当前鼠标悬停的手柄类型，或 None。"""
        if self._sel is None:
            return None
        handles, rot = self._handle_positions()
        if _dist(x, y, rot[1], rot[2]) <= _ROT_HIT_RADIUS:
            return "rot"
        for kind, hx, hy in handles:
            if _dist(x, y, hx, hy) <= _HIT_RADIUS:
                return kind
        return None

    def _on_release(self, event: tk.Event) -> None:
        if self._state == "dragging":
            self._finish_create(event)
            return
        if self._state in ("move-pending", "moving", "rotating") or \
                self._state.startswith("resize:") or self._state.startswith("quad:"):
            self._state = "selected"
            self._press = None
            self._sel0 = None
            self._quad0 = None
            self._draw_selected()
            if self._style == "bright" and self._background is not None:
                self._schedule_bright_refresh(force=True)   # 松手精确刷新
            self._show_action_bar()
            self._win.focus_force()  # 恢复键盘焦点，方向键可微调
            return

    def _on_double_click(self, _event: tk.Event) -> None:
        """双击确认：在调整态（含未发生移动的单击）双击即完成截图。"""
        if self._state in ("selected", "move-pending"):
            self._confirm()

    def _drag_create(self, event: tk.Event) -> None:
        """新建框选拖动中：实时绘制矩形 + 尺寸（bright 下框内即时明亮）。"""
        x0, y0 = self._start
        x1, y1 = event.x, event.y
        left, top = min(x0, x1), min(y0, y1)
        right, bottom = max(x0, x1), max(y0, y1)
        # bright：拖动中节流刷新亮度（边框原生跟手，亮度约 25fps 跟随）
        if self._style == "bright" and self._background is not None:
            self._sel = ((left + right) / 2, (top + bottom) / 2,
                         float(right - left), float(bottom - top), 0.0)
            self._schedule_bright_refresh()
        if self._rect_id is None:
            self._rect_id = self._canvas.create_rectangle(
                left, top, right, bottom,
                outline=_COLOR_SELECT, width=2, fill="", tags="sel")
        else:
            self._canvas.coords(self._rect_id, left, top, right, bottom)
        w, h = right - left, bottom - top
        self._canvas.delete("size")
        self._canvas.create_text((left + right) // 2, top - 10,
                                 text=f"{w} × {h}", fill="white",
                                 font=("Segoe UI", 10), tags="size")

    def _finish_create(self, event: tk.Event) -> None:
        """新建框选松手：有效则进入调整态；过小则提示/恢复原选区。"""
        x0, y0 = self._start
        x1, y1 = event.x, event.y
        self._start = None
        left, top = min(x0, x1), min(y0, y1)
        w = max(x0, x1) - left
        h = max(y0, y1) - top
        if w < MIN_SIZE or h < MIN_SIZE:
            if self._had_selection:
                self._restore_selection()
            else:
                self._state = "idle"
                self._flash_hint(f"Слишком маленькая область ({w}×{h}), выделите заново")
            return
        self._sel = (left + w / 2, top + h / 2, float(w), float(h), 0.0)
        if self._quad_mode:
            self._quad = self._rect_to_quad(self._sel)
        self._state = "selected"
        self._draw_selected()
        if self._style == "bright" and self._background is not None:
            self._schedule_bright_refresh(force=True)   # 松手精确刷新亮度
        if self._quad_mode:
            self._set_hint("Четырёхугольник: тяните 4 угла (верхняя сторона станет верхом), "
                           "кружок — поворот; Enter/двойной клик/«Готово» — подтвердить")
        else:
            self._set_hint(_HINT_SELECTED.format(w=w, h=h))
        self._show_action_bar()

    def _restore_selection(self) -> None:
        """框外重选过小时，恢复之前的选区。"""
        if self._prev_sel is None:
            self._state = "idle"
            return
        self._sel = self._prev_sel
        if self._quad_mode:
            self._quad = self._rect_to_quad(self._sel)
        self._state = "selected"
        self._draw_selected()
        w, h = self._sel[2], self._sel[3]
        self._set_hint(_HINT_SELECTED.format(w=round(w), h=round(h)))
        self._show_action_bar()

    # -- 移动 / 缩放 / 旋转 ------------------------------------------------

    def _clamp_center(self, cx: float, cy: float, w: float, h: float,
                      angle: float) -> tuple[float, float]:
        """把中心限制在屏幕内（保证旋转框完整可见，放不下则居中）。"""
        a = math.radians(angle)
        cos, sin = math.cos(a), math.sin(a)
        hw, hh = w / 2, h / 2
        ext_x = abs(hw * cos) + abs(hh * sin)
        ext_y = abs(hw * sin) + abs(hh * cos)
        if ext_x * 2 <= self._sw:
            cx = min(max(cx, ext_x), self._sw - ext_x)
        else:
            cx = self._sw / 2
        if ext_y * 2 <= self._sh:
            cy = min(max(cy, ext_y), self._sh - ext_y)
        else:
            cy = self._sh / 2
        return cx, cy

    def _move_to(self, event: tk.Event) -> None:
        if self._quad_mode and self._quad is not None and self._quad0 is not None:
            dx = event.x - self._press[0]
            dy = event.y - self._press[1]
            q = [(x + dx, y + dy) for x, y in self._quad0]
            x0, y0, x1, y1 = outer_bbox(q)
            cxc, cyc = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            cx2, cy2 = self._clamp_center(cxc, cyc, x1 - x0, y1 - y0, 0.0)
            off_x, off_y = cx2 - cxc, cy2 - cyc
            self._quad = [(x + dx + off_x, y + dy + off_y) for x, y in self._quad0]
            self._sync_sel_from_quad()
            return
        if self._sel0 is None:
            return
        cx0, cy0, w, h, angle = self._sel0
        cx, cy = cx0 + (event.x - self._press[0]), cy0 + (event.y - self._press[1])
        cx, cy = self._clamp_center(cx, cy, w, h, angle)
        self._sel = (cx, cy, w, h, angle)

    def _move_by(self, dx: float, dy: float) -> None:
        """方向键微调（1px / Shift 10px）。"""
        if self._sel is None:
            return
        if self._quad_mode and self._quad is not None:
            self._quad = [(x + dx, y + dy) for x, y in self._quad]
            self._sync_sel_from_quad()
            self._draw_selected()
            return
        cx, cy, w, h, angle = self._sel
        cx, cy = self._clamp_center(cx + dx, cy + dy, w, h, angle)
        self._sel = (cx, cy, w, h, angle)
        self._draw_selected()

    def _on_arrow(self, event: tk.Event) -> str | None:
        if self._state != "selected" or self._sel is None:
            return None
        step = 10 if (event.state & 0x0001) else 1
        ks = (event.keysym or "").lower()
        if ks == "left":
            self._move_by(-step, 0)
        elif ks == "right":
            self._move_by(step, 0)
        elif ks == "up":
            self._move_by(0, -step)
        elif ks == "down":
            self._move_by(0, step)
        else:
            return None
        return "break"  # 阻止其他默认处理

    def _apply_resize(self, kind: str, event: tk.Event) -> None:
        if self._sel is None:
            return
        cx, cy, w, h, angle = self._sel
        xaxis, yaxis = self._local_axes(angle)
        hw, hh = w / 2, h / 2
        mx, my = event.x, event.y
        if kind in ("tr", "tl", "bl", "br"):
            sx = 1 if kind in ("tr", "br") else -1
            sy = 1 if kind in ("bl", "br") else -1
            # 对侧角固定在世界空间
            fx = cx + (-sx) * hw * xaxis[0] + (-sy) * hh * yaxis[0]
            fy = cy + (-sx) * hw * xaxis[1] + (-sy) * hh * yaxis[1]
            dx, dy = mx - fx, my - fy
            dxl = dx * xaxis[0] + dy * xaxis[1]
            dyl = dx * yaxis[0] + dy * yaxis[1]
            hw2 = max(MIN_SIZE / 2, abs(dxl) / 2)
            hh2 = max(MIN_SIZE / 2, abs(dyl) / 2)
            self._sel = ((fx + mx) / 2, (fy + my) / 2, hw2 * 2, hh2 * 2, angle)
            return
        # 边手柄：对侧边固定（局部 +x/+y 为视觉右/下）
        if kind == "top":    # 拖上边（局部 -hh），固定下边（局部 +hh）
            mfx, mfy = cx + hh * yaxis[0], cy + hh * yaxis[1]
            d = (mx - mfx) * -yaxis[0] + (my - mfy) * -yaxis[1]
            hh2 = max(MIN_SIZE / 2, d / 2)
            self._sel = (mfx - hh2 * yaxis[0], mfy - hh2 * yaxis[1], w, hh2 * 2, angle)
        elif kind == "bottom":  # 拖下边（局部 +hh），固定上边（局部 -hh）
            mfx, mfy = cx - hh * yaxis[0], cy - hh * yaxis[1]
            d = (mx - mfx) * yaxis[0] + (my - mfy) * yaxis[1]
            hh2 = max(MIN_SIZE / 2, d / 2)
            self._sel = (mfx + hh2 * yaxis[0], mfy + hh2 * yaxis[1], w, hh2 * 2, angle)
        elif kind == "left":  # 拖左边（局部 -hw），固定右边（局部 +hw）
            mfx, mfy = cx + hw * xaxis[0], cy + hw * xaxis[1]
            d = (mx - mfx) * -xaxis[0] + (my - mfy) * -xaxis[1]
            hw2 = max(MIN_SIZE / 2, d / 2)
            self._sel = (mfx - hw2 * xaxis[0], mfy - hw2 * xaxis[1], hw2 * 2, h, angle)
        else:                # "right" 拖右边（局部 +hw），固定左边（局部 -hw）
            mfx, mfy = cx - hw * xaxis[0], cy - hw * xaxis[1]
            d = (mx - mfx) * xaxis[0] + (my - mfy) * xaxis[1]
            hw2 = max(MIN_SIZE / 2, d / 2)
            self._sel = (mfx + hw2 * xaxis[0], mfy + hw2 * xaxis[1], hw2 * 2, h, angle)

    def _apply_quad_drag(self, kind: str, event: tk.Event) -> None:
        """异形框：拖动单个角点（q0..q3）自由调节四边形。

        角点限制在画布内；若四边形退化为近 0 面积（含自交/翻面），保持该角点
        拖动前位置，避免透视矫正时共线/自交导致扭曲。
        """
        if self._quad is None or self._quad0 is None:
            return
        idx = int(kind[1])
        q = [list(p) for p in self._quad0]
        q[idx] = [min(max(float(event.x), 0.0), float(self._sw)),
                  min(max(float(event.y), 0.0), float(self._sh))]
        if _polygon_area(q) < _MIN_QUAD_AREA:
            return  # 退化：保持原四边形
        self._quad = [tuple(p) for p in q]
        self._sync_sel_from_quad()

    def _rotate_to(self, event: tk.Event, shift: bool) -> None:
        if self._sel is None:
            return
        if self._quad_mode and self._quad0 is not None:
            # 异形框：绕四边形质心旋转全部 4 个角点（质心旋转不变）
            cx, cy = self._quad_center()
            cur = math.atan2(event.y - cy, event.x - cx)
            delta = math.degrees(cur - self._start_angle)
            if shift:
                delta = round(delta / _ANGLE_SNAP) * _ANGLE_SNAP
            self._quad = self._rotate_quad_points(self._quad0, cx, cy, delta)
            self._sync_sel_from_quad()
            return
        cx, cy, w, h, _angle = self._sel
        cur = math.atan2(event.y - cy, event.x - cx)
        new_a = math.degrees(cur - self._start_angle) + self._sel_angle0
        new_a = ((new_a + 180) % 360) - 180   # 归一化到 (-180, 180]
        if shift:
            new_a = round(new_a / _ANGLE_SNAP) * _ANGLE_SNAP
        self._sel = (cx, cy, w, h, new_a)

    # -- 动作 -------------------------------------------------------------

    def _confirm(self) -> None:
        if self._state != "selected" or self._sel is None:
            return
        rx, ry = self._canvas.winfo_rootx(), self._canvas.winfo_rooty()
        if self._quad_mode and self._quad is not None:
            # 异形框：返回 4 个角点（屏幕坐标），顶边 quad[0]→quad[1] 作为正上方
            cx, cy = self._quad_center()
            x0, y0, x1, y1 = self._quad_bbox()
            q_screen = [(rx + px, ry + py) for px, py in self._quad]
            self._result = CaptureResult(
                int(round(rx + cx)), int(round(ry + cy)),
                max(MIN_SIZE, int(round(x1 - x0))),
                max(MIN_SIZE, int(round(y1 - y0))),
                0.0,
                quad=[(round(px, 2), round(py, 2)) for px, py in q_screen],
            )
        else:
            cx, cy, w, h, angle = self._sel
            self._result = CaptureResult(
                int(round(rx + cx)), int(round(ry + cy)),
                max(MIN_SIZE, int(round(w))), max(MIN_SIZE, int(round(h))),
                round(angle, 2),
            )
        self.destroy()

    def _reselect(self) -> None:
        """清除当前选区，回到待拖选态。"""
        self._hide_action_bar()
        self._state = "idle"
        self._sel = None
        self._prev_sel = None
        self._quad = None
        self._quad0 = None
        self._hover = None
        self._clear_selected_items()
        if self._style == "bright" and self._background is not None:
            self._show_full_dim()   # 无选区 → 整屏压暗
        self._set_hint(_HINT_IDLE)

    def _cancel(self) -> None:
        self._result = None
        self.destroy()
