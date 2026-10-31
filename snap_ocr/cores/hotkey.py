"""hotkey —— Windows 全局热键。

实现（DESIGN §2.1 / §3）：
    - 热键字符串 "ctrl+alt+z" → 修饰键位 + 虚拟键码（parse_hotkey）
    - WindowsHotkey：专用线程创建消息型窗口（HWND_MESSAGE）+ GetMessage 消息循环，
      接收 WM_HOTKEY 后在热键线程回调 on_activate（回调应把工作转发到主线程 EventBus）
    - 零第三方依赖：ctypes 调用 user32.RegisterHotKey / 消息循环

注意：RegisterHotKey 必须在拥有消息队列的线程上配合窗口使用，因此整条链路
（建类 → 建窗 → 注册 → 消息循环 → 注销）都在同一个守护线程内完成。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HWND_MESSAGE = ctypes.c_void_p(-3)  # 消息型窗口父句柄

_IDC_ARROW = ctypes.c_void_p(32512)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 声明签名，避免 32/64 位指针截断问题
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),  # wintypes 无 HCURSOR，用 HANDLE 即可
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wt.HANDLE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE


# ---------------------------------------------------------------------------
# 热键字符串解析
# ---------------------------------------------------------------------------

_NAMED_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "capslock": 0x14,
    "numlock": 0x90, "scrolllock": 0x91,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "numadd": 0x6B, "numsub": 0x6D, "nummul": 0x6A, "numdiv": 0x6F,
    "numdecimal": 0x6E,
    "comma": 0xBC, "period": 0xBE, "semicolon": 0xBA, "slash": 0xBF,
    "backslash": 0xDC, "minus": 0xBD, "equal": 0xBB, "bracketleft": 0xDB,
    "bracketright": 0xDD, "quote": 0xDE, "backquote": 0xC0,
}

_MOD_LABELS = ((MOD_CONTROL, "ctrl"), (MOD_ALT, "alt"),
               (MOD_SHIFT, "shift"), (MOD_WIN, "win"))


def _key_to_vk(name: str) -> int | None:
    """按键名 → Windows 虚拟键码；无法识别返回 None。"""
    name = name.lower()
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if name.startswith("f") and name[1:].isdigit():
        n = int(name[1:])
        if 1 <= n <= 24:
            return 0x70 + n - 1  # VK_F1..VK_F24
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    return None


@dataclass(frozen=True)
class HotkeySpec:
    """解析后的热键规格。mods 为修饰键位组合，vk 为虚拟键码。"""
    mods: int
    vk: int
    display: str  # 规范化展示，如 "ctrl+alt+z"


def parse_hotkey(spec: str) -> HotkeySpec | None:
    """把 "ctrl+alt+z" 解析为 HotkeySpec；非法输入返回 None。

    规则：至少一个非修饰键（字母/数字/F 键/命名键），修饰键可多个；
    允许裸键（如 "z"）；多个非修饰键 → 非法。
    """
    if not spec or not isinstance(spec, str):
        return None
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None

    mods = 0
    key_name: str | None = None
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "meta", "cmd", "super"):
            mods |= MOD_WIN
        else:
            if key_name is not None:
                return None  # 出现多个非修饰键 → 非法
            key_name = p

    if key_name is None:
        return None  # 只有修饰键 → 非法
    vk = _key_to_vk(key_name)
    if vk is None:
        return None

    parts_out = [label for mask, label in _MOD_LABELS if mods & mask]
    parts_out.append(key_name)
    return HotkeySpec(mods=mods, vk=vk, display="+".join(parts_out))


# ---------------------------------------------------------------------------
# 全局热键注册器
# ---------------------------------------------------------------------------


class WindowsHotkey:
    """Windows 全局热键：专用线程 + 消息型窗口 + GetMessage 循环。

    用法：:
        hk = WindowsHotkey(parse_hotkey("ctrl+alt+z"), lambda: bus.post(fn))
        ok = hk.register()      # 失败返回 False（被占用/非法）
        hk.unregister()
    """

    _CLASS_NAME = "SnapOcrHotkeyWnd"

    def __init__(self, spec: HotkeySpec, on_activate, hotkey_id: int = 0xBEEF) -> None:
        self._spec = spec
        self._on_activate = on_activate
        self._hotkey_id = hotkey_id
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._wndproc_ref: WNDPROC | None = None  # 防 GC
        self._ready = threading.Event()
        self._error: int | None = None

    # -- 公开接口 ---------------------------------------------------------

    def register(self, timeout: float = 5.0) -> bool:
        """注册热键；成功返回 True。已被占用/非法时返回 False。"""
        if self._thread and self._thread.is_alive():
            return True  # 已注册
        self._error = None
        self._ready.clear()
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._thread = threading.Thread(target=self._loop, name="snap-ocr-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            return False
        return self._error is None

    def unregister(self) -> None:
        """注销热键并退出消息循环线程。多次调用安全。"""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        thread, self._thread = self._thread, None
        if thread:
            thread.join(timeout=2.0)
        if self._hwnd:
            user32.UnregisterHotKey(self._hwnd, self._hotkey_id)
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None

    @property
    def spec(self) -> HotkeySpec:
        return self._spec

    # -- 内部实现 ---------------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_HOTKEY and wparam == self._hotkey_id:
            cb = self._on_activate
            if cb:
                try:
                    cb()
                except Exception:  # 回调异常不进入消息循环
                    pass
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _loop(self) -> None:
        """线程主体：建类 → 建消息窗 → 注册热键 → 消息循环。"""
        hinst = kernel32.GetModuleHandleW(None)

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, _IDC_ARROW)
        wc.lpszClassName = self._CLASS_NAME
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, self._CLASS_NAME, self._CLASS_NAME, 0,
            0, 0, 0, 0, HWND_MESSAGE, None, hinst, None,
        )
        if not hwnd:
            self._error = ctypes.get_last_error()
            self._ready.set()
            return
        self._hwnd = hwnd

        if not user32.RegisterHotKey(hwnd, self._hotkey_id, self._spec.mods, self._spec.vk):
            self._error = ctypes.get_last_error()
            self._ready.set()
            return

        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
