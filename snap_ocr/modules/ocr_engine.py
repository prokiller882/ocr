"""ocr_engine —— PP-OCRv6 ONNX 引擎封装。

对应 DESIGN §2.2：
    - OCRRecognizer 以懒加载单例持有 PPOCRv6（CPU）
    - warmup()：加载模型 + 跑一次完整 OCR（首帧提速），后台线程调用，不阻塞启动
    - recognize(img_bgr) -> list[OCRResult]：完整 OCR，输入须为 3 通道 BGR numpy 数组
    - 模型路径走 ppocrv6_onnx 包内 data/（安装后自动命中；打包态由
      --collect-all ppocrv6_onnx 解压到 _MEIPASS，包内相对路径天然正确）
"""
from __future__ import annotations

import threading

import numpy as np

from ..cores.logger import get_logger

logger = get_logger(__name__)

try:  # 延迟导入：允许在缺依赖时仍能加载其余模块（错误在 App 装配期上报）
    from ppocrv6_onnx import (
        InputError,
        ModelLoadError,
        OCRResult,
        PPOCRv6,
        PPOCRv6Error,
    )
    # 与引擎内部完全一致的裁剪/排序逻辑；未来 wheel 变更时回退到整图识别
    from ppocrv6_onnx._engine import crop_by_polys, sort_quad_boxes
except Exception as exc:  # pragma: no cover - 依赖缺失路径
    logger.warning("ppocrv6_onnx 导入失败（%s），OCR 功能不可用", exc)
    OCRResult = None  # type: ignore[assignment]
    PPOCRv6 = None  # type: ignore[assignment]
    PPOCRv6Error = ModelLoadError = InputError = RuntimeError  # type: ignore[assignment]
    crop_by_polys = sort_quad_boxes = None  # type: ignore[assignment]

__all__ = ["OCRRecognizer", "OCRResult"]


def preprocess_image(img_bgr: np.ndarray, scale_pct: int,
                     padding: int | None = None) -> np.ndarray:
    """识别前预处理：按 % 放大 + 边缘扩充（仅影响送入 OCR 的图）。

    - scale_pct: 放大倍数（100=原尺寸，>100 放大，<100 缩小）
    - padding: 边缘扩充像素（0=不扩充）；用边缘复制填充（replicate），
      让贴近边缘的文字也能被完整检测到
    返回放大/扩充后的 BGR ndarray。
    """
    scale_pct = max(10, min(int(scale_pct), 500))
    padding = max(0, int(padding or 0))
    out = np.asarray(img_bgr)
    if out.ndim == 2:  # 灰度 → 三通道（PPOCRv6 需要 BGR）
        out = np.stack([out] * 3, axis=-1)
    elif out.ndim == 3 and out.shape[2] == 4:  # RGBA → BGR
        out = out[:, :, :3]

    if scale_pct != 100:
        import cv2
        h, w = out.shape[:2]
        nw = max(1, int(round(w * scale_pct / 100.0)))
        nh = max(1, int(round(h * scale_pct / 100.0)))
        out = cv2.resize(out, (nw, nh), interpolation=cv2.INTER_LINEAR)

    if padding:
        import cv2
        out = cv2.copyMakeBorder(out, padding, padding, padding, padding,
                                 cv2.BORDER_REPLICATE)
    return out


class OCRRecognizer:
    """PPOCRv6 封装：懒加载、线程安全、可 warmup/close。

    支持自定义模型路径与检测阈值（来自设置）；空路径时使用内置模型。
    前处理（放大倍数 / 边缘扩充）在识别前应用，仅影响送入识别的图。
    """

    def __init__(self, model_dir: str | None = None, *,
                 det_model_path: str = "",
                 rec_model_path: str = "",
                 rec_char_dict_path: str = "",
                 det_thresh: float = 0.3,
                 det_box_thresh: float = 0.6,
                 det_unclip_ratio: float = 1.5,
                 rec_batch_size: int = 6,
                 preprocess_scale: int = 100,
                 preprocess_padding: int = 0) -> None:
        """
        model_dir: 可选；指定 det/rec/dict 所在目录（文件名为 det.onnx / rec.onnx /
                   chars.txt）。默认 None 使用 ppocrv6_onnx 包内内置模型。
        各 *_path 参数可单独覆盖模型/字典路径；空字符串表示用内置（或 model_dir）。
        preprocess_scale / preprocess_padding：识别前对图像做放大/边缘扩充。
        """
        self._model_dir = model_dir
        self._det_model_path = det_model_path
        self._rec_model_path = rec_model_path
        self._rec_char_dict_path = rec_char_dict_path
        self._det_thresh = float(det_thresh)
        self._det_box_thresh = float(det_box_thresh)
        self._det_unclip_ratio = float(det_unclip_ratio)
        self._rec_batch_size = int(rec_batch_size)
        self._scale_pct = int(preprocess_scale)
        self._padding = int(preprocess_padding)
        self._ocr: PPOCRv6 | None = None  # type: ignore[valid-type]
        self._lock = threading.Lock()
        self._closed = False

    def set_preprocess(self, scale_pct: int | None = None,
                       padding: int | None = None) -> None:
        """更新前处理参数（设置保存后调用）；None 表示保持原值。"""
        if scale_pct is not None:
            self._scale_pct = int(scale_pct)
        if padding is not None:
            self._padding = int(padding)

    def configure_models(self, *, det_model_path: str = "",
                         rec_model_path: str = "",
                         rec_char_dict_path: str = "",
                         det_thresh: float | None = None,
                         det_box_thresh: float | None = None,
                         det_unclip_ratio: float | None = None,
                         rec_batch_size: int | None = None) -> bool:
        """更新模型路径与阈值/批大小（设置保存后调用）。

        任一参数有变化都会关闭当前引擎并清空，按新配置重建（配合
        reload() 可立即重新加载）。返回是否有任何参数发生变更。
        """
        changed = (
            det_model_path != self._det_model_path
            or rec_model_path != self._rec_model_path
            or rec_char_dict_path != self._rec_char_dict_path
            or (det_thresh is not None and det_thresh != self._det_thresh)
            or (det_box_thresh is not None and det_box_thresh != self._det_box_thresh)
            or (det_unclip_ratio is not None
                and det_unclip_ratio != self._det_unclip_ratio)
            or (rec_batch_size is not None
                and rec_batch_size != self._rec_batch_size)
        )
        with self._lock:
            self._det_model_path = det_model_path
            self._rec_model_path = rec_model_path
            self._rec_char_dict_path = rec_char_dict_path
            if det_thresh is not None:
                self._det_thresh = float(det_thresh)
            if det_box_thresh is not None:
                self._det_box_thresh = float(det_box_thresh)
            if det_unclip_ratio is not None:
                self._det_unclip_ratio = float(det_unclip_ratio)
            if rec_batch_size is not None:
                self._rec_batch_size = int(rec_batch_size)
            if changed and self._ocr is not None:
                self._close_locked()
                logger.info("OCR 参数已变更，引擎将按新配置重建")
        return changed

    def reload(self) -> tuple[bool, str | None]:
        """按当前配置立即重建引擎并完整预热一次（设置保存后立即生效用）。

        应在子线程调用（模型加载耗时数秒）；返回 (ok, err_msg)，ok=False 时
        err_msg 为失败原因，便于 UI 即时提示新配置是否可用。
        """
        try:
            ocr = self._ensure()
            ocr(self._make_warmup_image())
            logger.info("OCR 引擎已按当前配置重新加载")
            return True, None
        except Exception as exc:  # noqa: BLE001 - 结果由调用方提示
            logger.warning("OCR 引擎重新加载失败: %s", exc)
            return False, str(exc)

    # -- 资源 -------------------------------------------------------------

    def _ensure(self) -> PPOCRv6:  # type: ignore[valid-type]
        """获取（必要时创建）引擎实例。模型加载失败抛 ModelLoadError。"""
        if self._closed:
            raise RuntimeError("OCRRecognizer has been closed")
        if self._ocr is not None:
            return self._ocr
        with self._lock:
            if self._ocr is None:
                if PPOCRv6 is None:  # pragma: no cover - 依赖缺失
                    raise RuntimeError("ppocrv6_onnx не установлен, распознавание невозможно")
                det = self._det_model_path or (f"{self._model_dir}/det.onnx"
                                               if self._model_dir else "")
                rec = self._rec_model_path or (f"{self._model_dir}/rec.onnx"
                                               if self._model_dir else "")
                dic = self._rec_char_dict_path or (
                    f"{self._model_dir}/chars.txt" if self._model_dir else "")
                self._ocr = PPOCRv6(
                    det_model_path=det or None,
                    rec_model_path=rec or None,
                    rec_char_dict_path=dic or None,
                    det_thresh=self._det_thresh,
                    det_box_thresh=self._det_box_thresh,
                    det_unclip_ratio=self._det_unclip_ratio,
                    rec_batch_size=self._rec_batch_size,
                )
                logger.info("OCR 引擎已加载（CPU）")
            return self._ocr

    def warmup(self) -> None:
        """用一张合成图跑一次完整 OCR，预热 ONNX session（首帧提速）。

        失败不抛出：warmup 是尽力而为，真实识别时仍会按错误态处理。
        """
        try:
            ocr = self._ensure()
            img = self._make_warmup_image()
            ocr(img)
            logger.info("OCR warmup 完成")
        except Exception as exc:  # noqa: BLE001 - warmup 容错
            logger.warning("OCR warmup 失败（不影响后续按需加载）: %s", exc)

    def recognize(self, img_bgr: np.ndarray) -> list[OCRResult]:  # type: ignore[valid-type]
        """完整 OCR。输入须为 3 通道 BGR ndarray（PPOCRv6 不接受灰度图）。

        识别前先应用前处理（放大倍数 + 边缘扩充）。原图不受影响。
        返回 list[OCRResult]；异常（InputError 等）向上抛出，由调用方按错误态处理。

        方向自适应：每个文本区域分别按 原样 / 旋转180° / 水平镜像 /
        水平镜像+旋转180° 识别，取置信度最高的结果。检测框与阅读顺序不变。
        （检测器已按最小外接矩形把任意角度摆正，故这四种覆盖所有正交方向组合。）
        """
        prepared = preprocess_image(img_bgr, self._scale_pct, self._padding)
        ocr = self._ensure()
        if prepared.shape[0] < 32 or prepared.shape[1] < 32:
            # 保持原有 InputError 行为（自定义路径的 detect 不做尺寸校验）
            return list(ocr(prepared))
        if crop_by_polys is None or sort_quad_boxes is None:
            # 兼容路径：旧版/未知 wheel 无内部裁剪函数时退回整图识别
            return list(ocr(prepared))

        boxes, _ = ocr.detect(prepared)
        if len(boxes) == 0:
            return []
        boxes = sort_quad_boxes(boxes)
        crops = crop_by_polys(prepared, boxes)
        pairs = [(box, crop) for box, crop in zip(boxes, crops)
                 if crop is not None]
        if not pairs:
            return []

        valid_boxes = [box for box, _ in pairs]
        valid_crops = [crop for _, crop in pairs]

        # 四种候选方向；numpy 视图不一定连续，交给引擎前统一转连续内存
        variants = (
            valid_crops,
            [np.ascontiguousarray(np.rot90(c, 2)) for c in valid_crops],
            [np.ascontiguousarray(c[:, ::-1]) for c in valid_crops],
            [np.ascontiguousarray(c[::-1, :]) for c in valid_crops],
        )
        texts, scores = ocr.recognize(
            [crop for group in variants for crop in group])

        n = len(valid_crops)
        results = []
        for i, box in enumerate(valid_boxes):
            best_text = texts[i]
            best_score = scores[i]
            for k in range(1, 4):
                j = k * n + i
                if scores[j] > best_score:
                    best_text = texts[j]
                    best_score = scores[j]
            results.append(OCRResult(text=best_text, score=best_score,
                                     box=box.tolist()))
        return results

    def close(self) -> None:
        with self._lock:
            self._close_locked()
            self._closed = True

    def _close_locked(self) -> None:
        """释放当前引擎（须在持锁时调用）；清空以便下次按新配置重建。"""
        if self._ocr is not None:
            try:
                self._ocr.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 OCR 引擎时异常（忽略）: %s", exc)
            self._ocr = None

    def __enter__(self) -> "OCRRecognizer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _make_warmup_image() -> np.ndarray:
        """生成含白色色块的合成 BGR 图，保证检测→识别链路都被触发。"""
        import cv2

        img = np.zeros((64, 64, 3), dtype=np.uint8)
        cv2.rectangle(img, (4, 10), (60, 30), (255, 255, 255), -1)
        cv2.rectangle(img, (4, 38), (40, 54), (255, 255, 255), -1)
        return img
