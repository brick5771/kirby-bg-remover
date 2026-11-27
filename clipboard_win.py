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


def save_image_file(
    pil_img: Image.Image,
    target: Path | str,
    fmt: str = "PNG",
    grayscale: bool = False,
    jpeg_quality: int = 88,
) -> None:
    """Save a PIL image as PNG (RGBA) or compact JPEG (white-composited L/RGB)."""
    fmt_upper = fmt.upper()
    if fmt_upper in ("JPEG", "JPG"):
        rgba = pil_img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        out = ImageOps.grayscale(bg) if grayscale else bg
        out.save(target, format="JPEG", quality=jpeg_quality, optimize=True)
    else:
        pil_img.convert("RGBA").save(target, format="PNG")


def copy_single_jpeg_to_clipboard(
    pil_img: Image.Image,
    filename_stem: str = "kirby_bw",
    grayscale: bool = True,
) -> Path:
    """Save a single compact JPEG into the temp batch folder and copy both the file URL
    (so Explorer / Line / Discord paste the small .jpg file) and bitmap image data."""
    root = _clipboard_batch_root()
    batch_dir = root / f"single_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    base = (filename_stem or "kirby_bw").strip()
    for ext in (".jpg", ".jpeg", ".png"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    safe = "".join("_" if c in '<>:"/\\|?*' else c for c in base).strip(" .") or "kirby_bw"
    out_path = batch_dir / f"{safe}.jpg"
    save_image_file(pil_img, out_path, fmt="JPEG", grayscale=grayscale)

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(out_path))])
    mime.setImageData(pil_to_qimage(pil_img))
    QGuiApplication.clipboard().setMimeData(mime)

    _prune_old_batches(root, keep=3)
    return out_path


def copy_multiple_pngs_to_clipboard(
    items: List[Tuple[Image.Image, str]],
    fmt: str = "PNG",
    grayscale: bool = False,
) -> List[Path]:
    """Save all processed images (PNG or JPEG) into a clean temp batch folder and place all
    file URLs (CF_HDROP) onto the Windows clipboard so Ctrl+V pastes ALL images at once into
    Explorer, Desktop, Line, Discord, PowerPoint, etc.
    """
    if not items:
        return []

    use_jpeg = fmt.upper() in ("JPEG", "JPG")
    ext = ".jpg" if use_jpeg else ".png"

    root = _clipboard_batch_root()
    batch_dir = root / f"batch_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[Path] = []
    used_names: set[str] = set()

    for idx, (pil_img, raw_name) in enumerate(items, start=1):
        base = (raw_name or f"kirby_{idx}").strip()
        for old_ext in (".png", ".jpg", ".jpeg"):
            if base.lower().endswith(old_ext):
                base = base[: -len(old_ext)]
                break
        # Sanitize Windows forbidden filename characters
        safe = "".join("_" if c in '<>:"/\\|?*' else c for c in base).strip(" .") or f"kirby_{idx}"
        candidate = f"{safe}{ext}"
        n = 2
        while candidate.lower() in used_names:
            candidate = f"{safe}_{n}{ext}"
            n += 1
        used_names.add(candidate.lower())

        out_path = batch_dir / candidate
        save_image_file(pil_img, out_path, fmt="JPEG" if use_jpeg else "PNG", grayscale=grayscale)
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
