"""win32 —— Windows 进程级原生配置（ctypes，零第三方依赖）。"""
from __future__ import annotations

import ctypes


def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知，保证 Tk 坐标与 ImageGrab 物理像素一致。

    显示器缩放 >100%（如 125%/150%）时，若进程不声明 DPI 感知，Windows 会
    把 Tk 的事件坐标/窗口尺寸虚拟化，而 ImageGrab.grab() 使用物理像素 →
    两者错位，导致框选位置不对、截图偏移。须在创建 Tk root 前调用。
    """
    try:
        # Windows 10 1703+：PROCESS_SYSTEM_DPI_AWARE = 1
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:  # noqa: BLE001 - 旧系统/无 shcore
        pass
    try:
        # Windows Vista+ 兜底
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass
