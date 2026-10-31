"""config —— SnapOCR 配置模型。

对应 PROJECT_SPEC.md §5 Config 数据字典，写入启动文件同目录 config.json。
默认值即 Spec 锁定值；config.json 不存在时用默认值并落盘。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .cores.paths import get_app_dir

# 枚举：识别语言（当前仅一档，MVP 不切换，预留扩展）
LANGUAGES = ("chinese_english",)


@dataclass
class Config:
    """应用配置。字段与 Spec §5 Config 一一对应，无未经确认的新增字段。"""
    hotkey: str = "ctrl+alt+z"          # 全局热键组合
    auto_copy: bool = False             # 识别完成后自动复制到剪贴板
    language: str = "chinese_english"   # 识别语言（仅展示，固定中英混合）
    history_limit: int = 50             # 历史记录最大条数（10–50），超出自动清理最旧
    overlay_style: str = "bright"       # 遮罩风格：bright=明亮选区（选区原亮+四周压暗，截图工具常见）；dark=整体半透明灰
    overlay_opacity: float = 0.4        # 暗化程度（bright 为选区外压暗强度，dark 为整体透明灰度；实现细节，不暴露到 UI）

    # -- OCR 前处理（仅影响送入识别的图，结果窗/历史仍用原图）---------------
    preprocess_scale: int = 100         # 放大倍数（%），100=原尺寸，仅识别用
    preprocess_padding: int = 0         # 边缘扩充（像素），0=不扩充，仅识别用

    # -- 模型参数（空字符串 = 使用内置模型；阈值在范围内可调）---------------
    ocr_det_model: str = ""             # 自定义检测模型 det.onnx 路径
    ocr_rec_model: str = ""             # 自定义识别模型 rec.onnx 路径
    ocr_char_dict: str = ""             # 自定义字典 chars.txt 路径
    ocr_det_thresh: float = 0.3         # 检测二值化阈值（0–1）
    ocr_det_box_thresh: float = 0.6     # 文本框得分阈值（0–1）
    ocr_det_unclip_ratio: float = 1.5   # 文本框外扩比例（≥1）
    ocr_rec_batch_size: int = 6         # 识别批大小（1–32）


def config_path() -> str:
    return str(get_app_dir() / "config.json")


def load() -> Config:
    """读取配置；文件缺失/损坏时回退默认值（不崩溃）。

    缺失时按 DESIGN §3 用默认值并落盘（首启生成 config.json）。
    """
    cfg = Config()
    path = Path(config_path())
    if not path.is_file():
        try:
            save(cfg)
        except OSError:
            pass  # 落盘失败不阻塞启动
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for fld in fields(Config):
                if fld.name in data:
                    setattr(cfg, fld.name, data[fld.name])
    except (OSError, ValueError):
        pass  # 读失败用默认值
    return cfg


def save(cfg: Config) -> None:
    """原子写配置：先写 .tmp 再替换，避免写一半损坏。"""
    path = get_app_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
