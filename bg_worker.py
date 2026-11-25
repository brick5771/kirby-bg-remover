"""Background removal & B&W conversion worker supporting single and multi-image batches."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QThread, Signal


@dataclass(frozen=True)
class ModelOption:
    key: str
    rembg_name: Optional[str]  # None means pure image filter (no rembg)
    short_label: str
    menu_label: str
    description: str
    grayscale: bool = False


MODEL_OPTIONS: Dict[str, ModelOption] = {
    "fast": ModelOption(
        key="fast",
        rembg_name="silueta",
        short_label="⚡ 快速",
        menu_label="⚡ 快速去背（預設・約 0.8 秒）",
        description="速度最快，適合日常截圖與一般物件",
    ),
    "balanced": ModelOption(
        key="balanced",
        rembg_name="isnet-general-use",
        short_label="✨ 均衡",
        menu_label="✨ 均衡去背（推薦・約 1.0 秒）",
        description="人物、動漫角色與商品邊緣更乾淨銳利",
    ),
    "hq": ModelOption(
        key="hq",
        rembg_name="birefnet-general-lite",
        short_label="💎 精細",
        menu_label="💎 超精細去背（髮絲極致・約 15 秒）",
        description="細緻髮絲與複雜輪廓專用，處理時間較長",
    ),
    "bw": ModelOption(
        key="bw",
        rembg_name=None,
        short_label="🖤 純黑白",
        menu_label="🖤 純轉黑白（不去背・瞬間完成）",
        description="將彩色圖片轉為高質感黑白灰階（保留原背景）",
        grayscale=True,
    ),
    "fast_bw": ModelOption(
        key="fast_bw",
        rembg_name="silueta",
        short_label="🌗 去背黑白",
        menu_label="🌗 去背＋黑白（快速去背並轉黑白）",
        description="同時去除背景並將主體轉為黑白透明 PNG",
        grayscale=True,
    ),
}

MODEL_ORDER = ["fast", "balanced", "hq", "bw", "fast_bw"]


def convert_to_bw_rgba(img: Image.Image) -> Image.Image:
    """Convert any PIL image to crisp perceptual grayscale while preserving alpha."""
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    gray = ImageOps.grayscale(rgba.convert("RGB"))
    return Image.merge("RGBA", (gray, gray, gray, alpha))


class SessionCache:
    """Thread-safe lazy cache of rembg ONNX sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, object] = {}

    def get_session(self, model_key: str):
        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        if opt.rembg_name is None:
            return None
        with self._lock:
            sess = self._sessions.get(opt.rembg_name)
            if sess is not None:
                return sess

        with self._lock:
            sess = self._sessions.get(opt.rembg_name)
            if sess is None:
                from rembg import new_session

                sess = new_session(opt.rembg_name)
                self._sessions[opt.rembg_name] = sess
            return sess

    def is_loaded(self, model_key: str) -> bool:
        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        if opt.rembg_name is None:
            return True
        with self._lock:
            return opt.rembg_name in self._sessions


def start_prewarm_daemon(cache: SessionCache, model_key: str = "fast") -> threading.Thread:
    """Preloads the default fast model on a daemon thread (safe on instant exit)."""

    def _run() -> None:
        try:
            cache.get_session(model_key)
        except Exception:
            pass

    t = threading.Thread(target=_run, name="kirby-prewarm", daemon=True)
    t.start()
    return t


class BatchProcessThread(QThread):
    """Processes one or multiple images (rembg and/or B&W) off the UI thread."""

    status_changed = Signal(str)
    finished_ok = Signal(object, float, str)  # (List[(PIL.Image RGBA, stem)], elapsed_s, model_key)
    finished_err = Signal(str)

    def __init__(
        self,
        cache: SessionCache,
        source_items: List[Tuple[Image.Image, str]],
        model_key: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._cache = cache
        self._source_items = source_items
        self._model_key = model_key

    def run(self) -> None:
        try:
            opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
            total = len(self._source_items)
            if total == 0:
                raise RuntimeError("沒有可處理的圖片")

            sess = None
            remove_fn = None
            if opt.rembg_name is not None:
                from rembg import remove

                remove_fn = remove
                if not self._cache.is_loaded(self._model_key):
                    self.status_changed.emit(f"載入 {opt.short_label} 模型中...")
                sess = self._cache.get_session(self._model_key)

            t0 = time.perf_counter()
            outputs: List[Tuple[Image.Image, str]] = []

            for idx, (src_img, stem) in enumerate(self._source_items, start=1):
                suffix = f" ({idx}/{total})" if total > 1 else ""
                if opt.rembg_name is None:
                    self.status_changed.emit(f"卡比把圖片轉成黑白中{suffix}...")
                elif opt.grayscale:
                    self.status_changed.emit(f"卡比去背並轉黑白中{suffix}...")
                else:
                    self.status_changed.emit(f"卡比努力消化去背中{suffix}...")

                img = ImageOps.exif_transpose(src_img)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")

                if remove_fn is None:
                    out_rgba = convert_to_bw_rgba(img)
                else:
                    result = remove_fn(img, session=sess)
                    if not isinstance(result, Image.Image):
                        raise RuntimeError("去背回傳格式異常")
                    out_rgba = result.convert("RGBA")
                    if opt.grayscale:
                        out_rgba = convert_to_bw_rgba(out_rgba)

                outputs.append((out_rgba, stem or f"kirby_{idx}"))

            elapsed = time.perf_counter() - t0
            self.finished_ok.emit(outputs, elapsed, self._model_key)
        except Exception as exc:
            self.finished_err.emit(f"處理失敗：{exc}")


# Backwards-compatible alias for single-image callers
class RemoveBgThread(BatchProcessThread):
    def __init__(
        self,
        cache: SessionCache,
        source_image: Image.Image,
        model_key: str,
        source_stem: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(cache, [(source_image, source_stem)], model_key, parent=parent)
