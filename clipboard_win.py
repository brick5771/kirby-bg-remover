"""Windows-friendly clipboard helpers preserving PNG transparency."""

from __future__ import annotations

import ctypes
import io
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, QMimeData
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

    # First set via Qt so CF_DIB / standard bitmap is populated cleanly
    qimg = pil_to_qimage(rgba)
    mime = QMimeData()
    mime.setImageData(qimg)
    mime.setData("image/png", png_bytes)
    mime.setData("PNG", png_bytes)
    QGuiApplication.clipboard().setMimeData(mime)

    # Also write the native Windows "PNG" clipboard format directly so Win32
    # consumers (PowerPoint, Discord, Office, Paint.NET, etc.) receive exact RGBA PNG.
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
        # Qt MIME clipboard already succeeded as fallback
        pass

    return True


def read_image_from_clipboard() -> Optional[Tuple[Image.Image, str]]:
    """Read an image or copied image file path from clipboard.

    Returns (pil_image, suggested_stem) or None if clipboard has no image.
    """
    cb = QGuiApplication.clipboard()
    mime = cb.mimeData()
    if mime is None:
        return None

    # 1) Copied image file in File Explorer
    if mime.hasUrls():
        for url in mime.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    try:
                        img = Image.open(p).convert("RGBA")
                        return img, p.stem
                    except Exception:
                        pass

    # 2) Raw PNG bytes if present
    for fmt in ("PNG", "image/png"):
        if mime.hasFormat(fmt):
            raw = bytes(mime.data(fmt))
            if raw:
                try:
                    img = Image.open(io.BytesIO(raw)).convert("RGBA")
                    return img, "clipboard_image"
                except Exception:
                    pass

    # 3) Standard QImage screenshot
    if mime.hasImage():
        qimg = cb.image()
        if not qimg.isNull():
            return qimage_to_pil(qimg), "screenshot"

    return None
