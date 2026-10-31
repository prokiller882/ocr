"""history —— 历史记录存储（单文件 JSON，截图 base64 内嵌）。

对应用户要求：所有历史数据存在一个 history.json 里，启动目录仅三个文件
（config.json / history.json / app.log），无任何子文件夹。
    - <启动目录>/history.json   全部历史记录（含截图 JPEG base64，自包含）
    - 生命周期：识别成功且文本非空时 add；删除/清空由用户触发；
      add 后超 history_limit 自动清理最旧
    - ts 格式 YYYYMMDD-HHMMSS（同一秒多条加 -NN 后缀）
    - 图片以 JPEG(base64) 内嵌：单文件即完整历史；文件体积随条目与截图
      尺寸增长（用户确认接受该取舍）
"""
from __future__ import annotations

import base64
import io
import json
import random
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from ..cores.logger import get_logger

logger = get_logger(__name__)

_TS_FORMAT = "%Y%m%d-%H%M%S"
_JPEG_QUALITY = 80   # 内嵌截图 JPEG 质量（与原全尺寸截图一致）


def _ts_sort_key(name: str) -> tuple[str, int]:
    """按 ts 排序的键：'YYYYMMDD-HHMMSS[-NN]' → (时间串, 序号)。

    同一秒内多条的 `-NN` 后缀比无后缀更新（NN>0），保证 add 顺序即新→旧。
    """
    parts = name.split("-")
    base = "".join(parts[:2])              # YYYYMMDDHHMMSS（定宽数字串，字典序=时间序）
    suffix = int(parts[2]) if len(parts) > 2 else 0
    return (base, suffix)


@dataclass
class HistoryEntry:
    """一条历史记录（图片以 base64 内嵌，字段仅用于历史展示/存储）。"""
    ts: str
    text: str
    score: float | None = None
    image_b64: str = ""      # JPEG base64（自包含，展示/看图共用同一张）
    box_count: int | None = None


def _encode_image(image_bgr: np.ndarray) -> str:
    """BGR ndarray → JPEG base64（三通道 RGB 编码，质量 _JPEG_QUALITY）。"""
    arr = np.asarray(image_bgr)
    if arr.ndim == 2:                    # 灰度 → 三通道
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] == 4:  # RGBA → RGB
        arr = arr[:, :, :3]
    img = Image.fromarray(arr[:, :, ::-1])  # BGR → RGB
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class HistoryStore:
    """单文件历史数据层：add / list / delete / clear，带上限清理与线程锁。

    内存中维护「新的在前」的条目列表，每次变更原子重写 history.json。
    """

    def __init__(self, root_dir: str | Path, limit: int = 200) -> None:
        self._root = Path(root_dir)
        self._file = self._root / "history.json"
        self._limit = int(limit)
        self._lock = threading.RLock()   # RLock：add→_trim→_save 嵌套加锁安全
        self._entries: list[HistoryEntry] = []   # 新的在前
        self._root.mkdir(parents=True, exist_ok=True)
        self._load()

    # -- 写 ---------------------------------------------------------------

    def add(self, text: str, score: float | None, image_bgr: np.ndarray,
            box_count: int | None = None) -> str:
        """追加一条记录（含截图 base64），返回 ts；超上限自动清理最旧。"""
        with self._lock:
            ts = self._unique_ts()
            entry = HistoryEntry(
                ts=ts,
                text=text,
                score=score,
                image_b64=_encode_image(image_bgr),
                box_count=box_count,
            )
            self._entries.insert(0, entry)
            self._trim_locked()
            self._save()
            logger.info("历史已保存: %s (%.0f字)", ts, len(text))
            return ts

    def set_limit(self, limit: int) -> None:
        """更新上限并立即清理超出部分（设置保存后调用）。"""
        self._limit = int(limit)
        with self._lock:
            self._trim_locked()
            self._save()

    # -- 读 ---------------------------------------------------------------

    def list_entries(self) -> list[HistoryEntry]:
        """按新→旧返回全部记录的副本。"""
        with self._lock:
            return [HistoryEntry(**asdict(e)) for e in self._entries]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def load_image(self, entry: HistoryEntry) -> Image.Image | None:
        """解码条目内嵌图片为 PIL Image（RGB），供展示/看图；失败返回 None。"""
        if not entry.image_b64:
            return None
        try:
            raw = base64.b64decode(entry.image_b64)
            return Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - 单条图片损坏不影响其余
            logger.warning("历史图片解码失败 %s: %s", entry.ts, exc)
            return None

    # -- 删 ---------------------------------------------------------------

    def delete(self, ts: str) -> bool:
        """删除单条记录；不存在返回 False。"""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.ts != ts]
            changed = len(self._entries) != before
            if changed:
                self._save()
            return changed

    def clear(self) -> None:
        """清空全部记录。"""
        with self._lock:
            self._entries = []
            self._save()

    # -- 内部 -------------------------------------------------------------

    def _load(self) -> None:
        """启动时读 history.json；缺失/损坏则从空列表开始（不崩溃）。"""
        if not self._file.is_file():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            raw = data.get("entries", []) if isinstance(data, dict) else data
            self._entries = [e for e in (self._parse(item) for item in raw)
                             if e is not None]
            self._entries.sort(key=lambda e: _ts_sort_key(e.ts), reverse=True)
            if len(self._entries) > self._limit:   # 读入即收敛到上限
                self._trim_locked()
        except (OSError, ValueError) as exc:
            logger.warning("读取历史失败 %s: %s", self._file, exc)
            self._entries = []

    @staticmethod
    def _parse(raw) -> HistoryEntry | None:
        if not isinstance(raw, dict):
            return None
        try:
            return HistoryEntry(
                ts=str(raw.get("ts", "")),
                text=str(raw.get("text", "")),
                score=raw.get("score"),
                image_b64=str(raw.get("image_b64", "")),
                box_count=raw.get("box_count"),
            )
        except Exception:  # noqa: BLE001 - 单条损坏跳过
            return None

    def _save(self) -> None:
        """原子重写 history.json（先写 .tmp 再替换，避免写一半损坏）。"""
        payload = json.dumps(
            {"entries": [asdict(e) for e in self._entries]},
            ensure_ascii=False, indent=2,
        )
        tmp = self._file.with_name("history.json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self._file)

    def _unique_ts(self) -> str:
        ts = datetime.now().strftime(_TS_FORMAT)
        taken = {e.ts for e in self._entries}
        while ts in taken:
            ts = f"{ts}-{random.randint(10, 99)}"
        return ts

    def _trim_locked(self) -> None:
        """超出 history_limit 时丢弃最旧（列表尾部）。须持锁调用。"""
        if len(self._entries) > self._limit:
            self._entries = self._entries[: self._limit]

    def __len__(self) -> int:
        return len(self._entries)
