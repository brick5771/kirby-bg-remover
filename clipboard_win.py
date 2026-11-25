"""Windows-friendly clipboard helpers supporting single PNG transparency & multi-file batch copy."""

from __future__ import annotations

import ctypes
import io
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageOps
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QUrl
from PySide6.QtGui import QGuiApplication, QImage

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
}


def pil_to_qimage(pil_img: Image.Image) -> QImage:
    """Convert PIL Image (any mode) to an independent RGBA8888 QImage."""
    rgba = pil_img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimg = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888)
    return qimg.copy()


def qimage_to_pil(qimg: QImage) -> Image.Image:
    """Convert QImage to PIL RGBA Image."""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.ReadWrite)
    qimg.save(buf, "PNG")
    raw = bytes(buf.data())
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def _clipboard_batch_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "KirbyBgRemover" / "clipboard_batch"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _prune_old_batches(root: Path, keep: int = 3) -> None:
    try:
        dirs = sorted(
            [d for d in root.iterdir() if d.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in dirs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def copy_transparent_png_to_clipboard(pil_img: Image.Image) -> bool:
    """Put lossless RGBA PNG + DIB fallback onto the Windows clipboard.

    Using Win32 RegisterClipboardFormatW('PNG') together with Qt's image
    ensures transparency is preserved in Discord, PowerPoint, Line, Figma,
    Photoshop, Chrome, Word, etc., while legacy apps still get a DIB bitmap.
    """
    rgba = pil_img.convert("RGBA")
    png_buf = io.BytesIO()
    rgba.save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()

    qimg = pil_to_qimage(rgba)
    mime = QMimeData()
    mime.setImageData(qimg)
    mime.setData("image/png", png_bytes)
    mime.setData("PNG", png_bytes)
    QGuiApplication.clipboard().setMimeData(mime)

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        GMEM_MOVEABLE = 0x0002
        user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.CloseClipboard.restype = ctypes.c_bool
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool

        cf_png = user32.RegisterClipboardFormatW("PNG")
        if cf_png and user32.OpenClipboard(None):
            try:
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(png_bytes))
                if h_mem:
                    ptr = kernel32.GlobalLock(h_mem)
                    if ptr:
                        ctypes.memmove(ptr, png_bytes, len(png_bytes))
                        kernel32.GlobalUnlock(h_mem)
                        user32.SetClipboardData(cf_png, h_mem)
            finally:
                user32.CloseClipboard()
    except Exception:
        pass

    return True


def copy_multiple_pngs_to_clipboard(items: List[Tuple[Image.Image, str]]) -> List[Path]:
    """Save all processed PNGs into a clean temp batch folder and place all file URLs (CF_HDROP)
    onto the Windows clipboard so Ctrl+V pastes ALL images at once into Explorer, Desktop, Line,
    Discord, PowerPoint, etc.
    """
    if not items:
        return []

    root = _clipboard_batch_root()
    batch_dir = root / f"batch_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[Path] = []
    used_names: set[str] = set()

    for idx, (pil_img, raw_name) in enumerate(items, start=1):
        base = (raw_name or f"kirby_{idx}").strip()
        if base.lower().endswith(".png"):
            base = base[:-4]
        # Sanitize Windows forbidden filename characters
        safe = "".join("_" if c in '<>:"/\\|?*' else c for c in base).strip(" .") or f"kirby_{idx}"
        candidate = f"{safe}.png"
        n = 2
        while candidate.lower() in used_names:
            candidate = f"{safe}_{n}.png"
            n += 1
        used_names.add(candidate.lower())

        out_path = batch_dir / candidate
        pil_img.convert("RGBA").save(out_path, format="PNG")
        saved_paths.append(out_path)

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in saved_paths])
    QGuiApplication.clipboard().setMimeData(mime)

    _prune_old_batches(root, keep=3)
    return saved_paths


def read_images_from_clipboard() -> List[Tuple[Image.Image, str]]:
    """Read one or MULTIPLE images (or copied image file paths) from the clipboard.

    Returns a list of (pil_rgba_image, suggested_stem). Empty list if no images found.
    """
    cb = QGuiApplication.clipboard()
    mime = cb.mimeData()
    if mime is None:
        return []

    # 1) One or multiple copied image files from File Explorer / Desktop
    if mime.hasUrls():
        found: List[Tuple[Image.Image, str]] = []
        for url in mime.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    try:
                        img = ImageOps.exif_transpose(Image.open(p)).convert("RGBA")
                        found.append((img, p.stem))
                    except Exception:
                        pass
        if found:
            return found

    # 2) Raw PNG bytes if present
    for fmt in ("PNG", "image/png"):
        if mime.hasFormat(fmt):
            raw = bytes(mime.data(fmt))
            if raw:
                try:
                    img = Image.open(io.BytesIO(raw)).convert("RGBA")
                    return [(img, "clipboard_image")]
                except Exception:
                    pass

    # 3) Standard QImage screenshot
    if mime.hasImage():
        qimg = cb.image()
        if not qimg.isNull():
            return [(qimage_to_pil(qimg), "screenshot")]

    return []


def read_image_from_clipboard() -> Optional[Tuple[Image.Image, str]]:
    """Backwards-compatible single-image reader."""
    items = read_images_from_clipboard()
    return items[0] if items else None
