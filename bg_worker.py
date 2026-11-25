"""Background removal worker powered by local rembg ONNX models."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QThread, Signal


@dataclass(frozen=True)
class ModelOption:
    key: str
    rembg_name: str
    short_label: str
    menu_label: str
    description: str


MODEL_OPTIONS: Dict[str, ModelOption] = {
    "fast": ModelOption(
        key="fast",
        rembg_name="silueta",
        short_label="⚡ 快速",
        menu_label="⚡ 快速模式（預設・約 0.8 秒）",
        description="速度最快，適合日常截圖與一般物件",
    ),
    "balanced": ModelOption(
        key="balanced",
        rembg_name="isnet-general-use",
        short_label="✨ 均衡",
        menu_label="✨ 均衡模式（推薦・約 1.0 秒）",
        description="人物、動漫角色與商品邊緣更乾淨銳利",
    ),
    "hq": ModelOption(
        key="hq",
        rembg_name="birefnet-general-lite",
        short_label="💎 精細",
        menu_label="💎 超精細模式（髮絲極致・約 15 秒）",
        description="細緻髮絲與複雜輪廓專用，處理時間較長",
    ),
}

MODEL_ORDER = ["fast", "balanced", "hq"]


class SessionCache:
    """Thread-safe lazy cache of rembg ONNX sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, object] = {}

    def get_session(self, model_key: str):
        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        with self._lock:
            sess = self._sessions.get(opt.rembg_name)
            if sess is not None:
                return sess

        # Load outside lock? Keep locked per model load so duplicate loads don't race.
        with self._lock:
            sess = self._sessions.get(opt.rembg_name)
            if sess is None:
                from rembg import new_session

                sess = new_session(opt.rembg_name)
                self._sessions[opt.rembg_name] = sess
            return sess

    def is_loaded(self, model_key: str) -> bool:
        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
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


class RemoveBgThread(QThread):
    """Runs rembg background removal off the UI thread."""

    status_changed = Signal(str)
    finished_ok = Signal(object, float, str, str)  # (PIL.Image RGBA, elapsed_s, model_key, stem)
    finished_err = Signal(str)

    def __init__(
        self,
        cache: SessionCache,
        source_image: Image.Image,
        model_key: str,
        source_stem: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._cache = cache
        self._source_image = source_image
        self._model_key = model_key
        self._source_stem = source_stem

    def run(self) -> None:
        try:
            from rembg import remove

            if not self._cache.is_loaded(self._model_key):
                opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
                self.status_changed.emit(f"載入 {opt.short_label} 模型中...")
            sess = self._cache.get_session(self._model_key)

            self.status_changed.emit("卡比努力消化去背中...")
            t0 = time.perf_counter()
            img = ImageOps.exif_transpose(self._source_image)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            result = remove(img, session=sess)
            if not isinstance(result, Image.Image):
                raise RuntimeError("去背回傳格式異常")
            result_rgba = result.convert("RGBA")
            elapsed = time.perf_counter() - t0
            self.finished_ok.emit(result_rgba, elapsed, self._model_key, self._source_stem)
        except Exception as exc:
            self.finished_err.emit(f"去背失敗：{exc}")
