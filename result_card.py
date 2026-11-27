"""Floating Kirby-styled cutout result preview & save card (single & multi-image batch)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image
from PySide6.QtCore import QPoint, QRect, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from bg_worker import MODEL_OPTIONS, MODEL_ORDER, convert_to_bw_rgba
from clipboard_win import (
    IMAGE_EXTENSIONS,
    copy_multiple_pngs_to_clipboard,
    copy_single_jpeg_to_clipboard,
    copy_transparent_png_to_clipboard,
    pil_to_qimage,
    read_images_from_clipboard,
    save_image_file,
)
from sound_fx import SoundManager


class PreviewCanvas(QWidget):
    """Displays the transparent cutout over checkerboard / light / dark backdrop."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._bg_mode = "checker"  # "checker" | "light" | "dark"
        self.setMinimumSize(326, 226)

    def set_image(self, pil_img: Image.Image) -> None:
        self._pixmap = QPixmap.fromImage(pil_to_qimage(pil_img))
        self.update()

    def set_bg_mode(self, mode: str) -> None:
        self._bg_mode = mode
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = self.rect().adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 14, 14)
        p.setClipPath(clip)

        if self._bg_mode == "light":
            p.fillRect(rect, QColor("#FFFFFF"))
        elif self._bg_mode == "dark":
            p.fillRect(rect, QColor("#1E1D2B"))
        else:
            tile = 14
            c1 = QColor("#F6F3F8")
            c2 = QColor("#E4DCE9")
            p.fillRect(rect, c1)
            for y in range(rect.top(), rect.bottom(), tile):
                row_idx = (y - rect.top()) // tile
                for x in range(rect.left(), rect.right(), tile):
                    col_idx = (x - rect.left()) // tile
                    if (row_idx + col_idx) % 2 == 1:
                        p.fillRect(x, y, tile, tile, c2)

        if self._pixmap and not self._pixmap.isNull():
            pad = 12
            avail = rect.adjusted(pad, pad, -pad, -pad)
            scaled = self._pixmap.scaled(
                avail.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            dx = avail.x() + (avail.width() - scaled.width()) // 2
            dy = avail.y() + (avail.height() - scaled.height()) // 2
            p.drawPixmap(dx, dy, scaled)

        p.setClipping(False)
        p.setPen(QPen(QColor(218, 160, 192, 110), 1.2))
        p.drawRoundedRect(rect, 14, 14)


class ResultCardWindow(QWidget):
    """Frameless rounded floating card showing single or multiple processed images."""

    model_selected = Signal(str, bool)  # (model_key, rerun_current)
    files_dropped = Signal(list)        # List[str]
    images_pasted = Signal(list)        # List[(PIL.Image, stem)]
    pick_new_requested = Signal()

    def __init__(self, sound: SoundManager, parent=None) -> None:
        super().__init__(parent)
        self._sound = sound
        self._base_items: List[Tuple[Image.Image, str]] = []
        self._display_items: List[Tuple[Image.Image, str]] = []
        self._current_index = 0
        self._model_key = "fast"
        self._bw_toggled = False
        desktop_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        self._last_save_dir = desktop_dir or str(Path.home() / "Desktop")
        self._drag_offset: Optional[QPoint] = None
        self._always_on_top = True
        self._thumb_buttons: List[QPushButton] = []

        self.setWindowTitle("星之卡比處理結果")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setFixedWidth(382)

        self._build_ui()
        self._setup_shortcuts()

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)

    @property
    def _result_pil(self) -> Optional[Image.Image]:
        if 0 <= self._current_index < len(self._display_items):
            return self._display_items[self._current_index][0]
        return None

    @property
    def _is_batch(self) -> bool:
        return len(self._display_items) > 1

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 16)

        self._card = QFrame(self)
        self._card.setObjectName("KirbyCard")
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(138, 45, 92, 72))
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)

        # --- Header (title + timing badge + top-right model selector + close) ---
        header = QHBoxLayout()
        header.setSpacing(6)

        self._title_lbl = QLabel("✨ 去背完成！")
        self._title_lbl.setObjectName("CardTitle")
        header.addWidget(self._title_lbl)

        self._meta_lbl = QLabel("")
        self._meta_lbl.setObjectName("MetaBadge")
        header.addWidget(self._meta_lbl)
        header.addStretch(1)

        self._model_btn = QPushButton("⚡ 快速 ▾")
        self._model_btn.setObjectName("ModelPill")
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.setToolTip("切換去背／黑白模式（切換後自動重新處理目前所有圖片）")
        self._model_btn.clicked.connect(self._open_model_menu)
        header.addWidget(self._model_btn)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setFixedSize(26, 26)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("收合結果視窗（Esc）")
        close_btn.clicked.connect(self.hide)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # --- Preview Canvas ---
        self._preview = PreviewCanvas(self._card)
        layout.addWidget(self._preview)

        # --- Multi-image Thumbnail Bar (only visible when batch > 1) ---
        self._batch_bar = QWidget(self._card)
        batch_vbox = QVBoxLayout(self._batch_bar)
        batch_vbox.setContentsMargins(0, 0, 0, 0)
        batch_vbox.setSpacing(5)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(5)

        self._prev_btn = QPushButton("◀")
        self._prev_btn.setObjectName("NavBtn")
        self._prev_btn.setFixedSize(24, 24)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(lambda: self._step_index(-1))
        nav_row.addWidget(self._prev_btn)

        self._idx_lbl = QLabel("1 / 1")
        self._idx_lbl.setObjectName("BatchCounter")
        nav_row.addWidget(self._idx_lbl)

        self._next_img_btn = QPushButton("▶")
        self._next_img_btn.setObjectName("NavBtn")
        self._next_img_btn.setFixedSize(24, 24)
        self._next_img_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_img_btn.clicked.connect(lambda: self._step_index(1))
        nav_row.addWidget(self._next_img_btn)

        nav_row.addStretch(1)

        self._copy_one_btn = QPushButton("複製此張")
        self._copy_one_btn.setObjectName("MiniActionBtn")
        self._copy_one_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_one_btn.setToolTip("只複製目前檢視的這一張透明圖")
        self._copy_one_btn.clicked.connect(self.copy_current_only)
        nav_row.addWidget(self._copy_one_btn)

        self._save_one_btn = QPushButton("存此張")
        self._save_one_btn.setObjectName("MiniActionBtn")
        self._save_one_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_one_btn.setToolTip("只另存目前檢視的這一張")
        self._save_one_btn.clicked.connect(self.save_current_only)
        nav_row.addWidget(self._save_one_btn)

        batch_vbox.addLayout(nav_row)

        self._thumb_scroll = QScrollArea(self._batch_bar)
        self._thumb_scroll.setObjectName("ThumbScroll")
        self._thumb_scroll.setWidgetResizable(True)
        self._thumb_scroll.setFixedHeight(58)
        self._thumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._thumb_container = QWidget()
        self._thumb_layout = QHBoxLayout(self._thumb_container)
        self._thumb_layout.setContentsMargins(2, 2, 2, 2)
        self._thumb_layout.setSpacing(6)
        self._thumb_layout.addStretch(1)
        self._thumb_scroll.setWidget(self._thumb_container)
        batch_vbox.addWidget(self._thumb_scroll)

        self._batch_bar.setVisible(False)
        layout.addWidget(self._batch_bar)

        # --- Background preview switcher + instant B&W toggle + dimensions ---
        subbar = QHBoxLayout()
        subbar.setSpacing(4)

        self._bg_btns: dict[str, QPushButton] = {}
        for mode, text in (("checker", "透明格"), ("light", "淺白"), ("dark", "深黑")):
            btn = QPushButton(text)
            btn.setObjectName("BgToggle")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, m=mode: self._select_bg(m))
            subbar.addWidget(btn)
            self._bg_btns[mode] = btn
        self._bg_btns["checker"].setChecked(True)

        self._bw_btn = QPushButton("🖤 轉黑白")
        self._bw_btn.setObjectName("BwToggle")
        self._bw_btn.setCheckable(True)
        self._bw_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bw_btn.setToolTip("一鍵切換彩色／黑白輸出（套用到全部圖片）")
        self._bw_btn.clicked.connect(self._toggle_bw_filter)
        subbar.addWidget(self._bw_btn)

        subbar.addStretch(1)
        self._dim_lbl = QLabel("")
        self._dim_lbl.setObjectName("SubText")
        subbar.addWidget(self._dim_lbl)
        layout.addLayout(subbar)

        # --- Toast feedback line ---
        self._toast_lbl = QLabel("")
        self._toast_lbl.setObjectName("ToastLbl")
        self._toast_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._toast_lbl.setVisible(False)
        layout.addWidget(self._toast_lbl)

        # --- Main Action Buttons ---
        actions = QHBoxLayout()
        actions.setSpacing(8)

        self._save_btn = QPushButton("💾 儲存去背圖")
        self._save_btn.setObjectName("PrimaryBtn")
        self._save_btn.setMinimumHeight(40)
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setToolTip("儲存結果 PNG（Ctrl+S）")
        self._save_btn.clicked.connect(self.save_image)
        actions.addWidget(self._save_btn, 3)

        self._copy_btn = QPushButton("📋 複製")
        self._copy_btn.setObjectName("SecondaryBtn")
        self._copy_btn.setMinimumHeight(40)
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setToolTip("複製到剪貼簿（Ctrl+C）；多張時一次複製全部圖片檔")
        self._copy_btn.clicked.connect(self.copy_image)
        actions.addWidget(self._copy_btn, 2)

        self._next_btn = QPushButton("🍓 換圖")
        self._next_btn.setObjectName("GhostBtn")
        self._next_btn.setMinimumHeight(40)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setToolTip("選擇一張或多張圖片餵給卡比")
        self._next_btn.clicked.connect(self._on_pick_new)
        actions.addWidget(self._next_btn, 1)

        layout.addLayout(actions)

        self.setStyleSheet(
            """
            QFrame#KirbyCard {
                background-color: rgba(255, 249, 252, 248);
                border: 2px solid #F7A8C8;
                border-radius: 20px;
            }
            QLabel#CardTitle {
                color: #881C50;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 13.5px;
                font-weight: 700;
            }
            QLabel#MetaBadge {
                color: #9A4A73;
                background-color: #FCE4F0;
                border-radius: 8px;
                padding: 2px 7px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#ModelPill {
                background-color: #FFF0F6;
                color: #A81F62;
                border: 1.5px solid #F5A3C7;
                border-radius: 12px;
                padding: 3px 10px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton#ModelPill:hover {
                background-color: #FFE0EE;
                border-color: #EB6CA7;
            }
            QPushButton#CloseBtn {
                background-color: transparent;
                color: #B06A8E;
                border: none;
                border-radius: 13px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#CloseBtn:hover {
                background-color: #FCDCEB;
                color: #8A1A4F;
            }
            QPushButton#NavBtn {
                background-color: #FFEAF4;
                color: #A81F62;
                border: 1px solid #F3A3C8;
                border-radius: 12px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton#NavBtn:hover {
                background-color: #FFD6EA;
            }
            QLabel#BatchCounter {
                color: #881C50;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
                font-weight: 700;
                padding: 0 3px;
            }
            QPushButton#MiniActionBtn {
                background-color: #F9EFF5;
                color: #8E4A6E;
                border: 1px solid #E8CEE0;
                border-radius: 9px;
                padding: 2px 8px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 10.5px;
                font-weight: 600;
            }
            QPushButton#MiniActionBtn:hover {
                background-color: #FFE2F0;
                color: #9C1758;
            }
            QScrollArea#ThumbScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#ThumbScroll > QWidget > QWidget {
                background: transparent;
            }
            QPushButton#ThumbItem {
                background-color: #F6ECF1;
                border: 2px solid #E8D0DD;
                border-radius: 10px;
                padding: 2px;
            }
            QPushButton#ThumbItem:hover {
                border-color: #F28CBA;
            }
            QPushButton#ThumbItem:checked {
                background-color: #FFE4F1;
                border: 2.5px solid #EC5C9D;
            }
            QLabel#SubText {
                color: #8E687E;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
            }
            QPushButton#BgToggle {
                background-color: #F6ECF1;
                color: #7D556B;
                border: 1px solid #E8D0DD;
                border-radius: 9px;
                padding: 2px 8px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
            }
            QPushButton#BgToggle:hover {
                background-color: #FDE5F0;
            }
            QPushButton#BgToggle:checked {
                background-color: #EC5C9D;
                color: #FFFFFF;
                border-color: #D94388;
                font-weight: 700;
            }
            QPushButton#BwToggle {
                background-color: #F2EFF5;
                color: #4A3E52;
                border: 1px solid #D8CEE0;
                border-radius: 9px;
                padding: 2px 8px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#BwToggle:hover {
                background-color: #E7E0EE;
            }
            QPushButton#BwToggle:checked {
                background-color: #2D2A36;
                color: #FFFFFF;
                border-color: #1A1822;
                font-weight: 700;
            }
            QLabel#ToastLbl {
                color: #1F7A4D;
                background-color: #E3F9ED;
                border: 1px solid #A8E5C5;
                border-radius: 9px;
                padding: 4px 8px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#PrimaryBtn {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #FF75B5, stop:1 #E84393);
                color: #FFFFFF;
                border: none;
                border-radius: 13px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 12.5px;
                font-weight: 700;
                padding: 0 10px;
            }
            QPushButton#PrimaryBtn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #FF88C0, stop:1 #F2509E);
            }
            QPushButton#PrimaryBtn:pressed {
                background-color: #D43482;
            }
            QPushButton#SecondaryBtn {
                background-color: #FFEAF4;
                color: #A81F62;
                border: 1.5px solid #F3A3C8;
                border-radius: 13px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 12.5px;
                font-weight: 700;
                padding: 0 8px;
            }
            QPushButton#SecondaryBtn:hover {
                background-color: #FFD9EB;
            }
            QPushButton#GhostBtn {
                background-color: #F8F1F5;
                color: #7A546A;
                border: 1px solid #E5D1DC;
                border-radius: 13px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 12px;
                font-weight: 600;
                padding: 0 6px;
            }
            QPushButton#GhostBtn:hover {
                background-color: #F2E2EB;
            }
            """
        )

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save_image)
        QShortcut(QKeySequence.StandardKey.Copy, self, activated=self.copy_image)
        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self._paste_from_clipboard)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self._step_index(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self._step_index(1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide)

    def set_always_on_top(self, on_top: bool) -> None:
        self._always_on_top = on_top
        was_visible = self.isVisible()
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def set_model_key(self, model_key: str) -> None:
        self._model_key = model_key
        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        self._model_btn.setText(f"{opt.short_label} ▾")

    def show_result(
        self,
        pil_img: Image.Image,
        elapsed_s: float,
        model_key: str,
        source_stem: str,
        kirby_rect: QRect,
    ) -> None:
        """Convenience wrapper for displaying a single result."""
        self.show_results([(pil_img, source_stem)], elapsed_s, model_key, kirby_rect)

    def show_results(
        self,
        items: List[Tuple[Image.Image, str]],
        elapsed_s: float,
        model_key: str,
        kirby_rect: QRect,
    ) -> None:
        if not items:
            return
        self._base_items = [(img, stem or f"kirby_{i}") for i, (img, stem) in enumerate(items, 1)]
        self._display_items = list(self._base_items)
        self._current_index = 0
        self.set_model_key(model_key)

        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        self._bw_toggled = False
        self._bw_btn.setVisible(not opt.grayscale)
        self._bw_btn.setChecked(False)

        self._meta_lbl.setText(f"{elapsed_s:.1f}s" if elapsed_s >= 0.1 else "<0.1s")
        self._toast_lbl.setVisible(False)
        self._rebuild_thumbnails()
        self._select_index(0, play_sound=False)
        self._refresh_labels_for_mode()
        self.adjustSize()

        # Position cleanly beside Kirby's back (left of Kirby), staying inside monitor bounds
        screen = QApplication.screenAt(kirby_rect.center()) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        card_w = self.width()
        card_h = self.height()

        x = kirby_rect.left() - card_w + 18
        if x < avail.left() + 8:
            x = kirby_rect.right() - 18
        x = max(avail.left() + 8, min(x, avail.right() - card_w - 8))

        y = kirby_rect.center().y() - card_h // 2
        y = max(avail.top() + 8, min(y, avail.bottom() - card_h - 8))

        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def _rebuild_thumbnails(self) -> None:
        for btn in self._thumb_buttons:
            self._thumb_layout.removeWidget(btn)
            btn.deleteLater()
        self._thumb_buttons.clear()

        is_batch = len(self._display_items) > 1
        self._batch_bar.setVisible(is_batch)
        if not is_batch:
            return

        # Insert thumbnail buttons before the trailing stretch
        for idx, (pil_img, stem) in enumerate(self._display_items):
            btn = QPushButton(self._thumb_container)
            btn.setObjectName("ThumbItem")
            btn.setCheckable(True)
            btn.setFixedSize(46, 46)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{idx + 1}. {stem}")
            thumb = pil_img.copy()
            thumb.thumbnail((38, 38), Image.Resampling.LANCZOS)
            btn.setIcon(QIcon(QPixmap.fromImage(pil_to_qimage(thumb))))
            btn.setIconSize(QSize(38, 38))
            btn.clicked.connect(lambda _c=False, i=idx: self._select_index(i, play_sound=True))
            self._thumb_layout.insertWidget(idx, btn)
            self._thumb_buttons.append(btn)

    def _select_index(self, idx: int, play_sound: bool = False) -> None:
        if not self._display_items:
            return
        idx = max(0, min(len(self._display_items) - 1, idx))
        self._current_index = idx
        if play_sound:
            self._sound.play("click")

        img, stem = self._display_items[idx]
        self._preview.set_image(img)
        self._dim_lbl.setText(f"{img.width} × {img.height}")
        self._idx_lbl.setText(f"{idx + 1} / {len(self._display_items)}")
        for i, btn in enumerate(self._thumb_buttons):
            btn.setChecked(i == idx)
        if 0 <= idx < len(self._thumb_buttons):
            self._thumb_scroll.ensureWidgetVisible(self._thumb_buttons[idx])

    def _step_index(self, delta: int) -> None:
        if not self._is_batch:
            return
        new_idx = (self._current_index + delta) % len(self._display_items)
        self._select_index(new_idx, play_sound=True)

    def _output_suffix(self) -> str:
        opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
        if self._model_key == "bw":
            return "_bw"
        if opt.grayscale or self._bw_toggled:
            return "_nobg_bw"
        return "_nobg"

    def _prefers_jpeg(self) -> bool:
        """Pure B&W (without background removal) defaults to compact JPEG."""
        return self._model_key == "bw"

    def _is_grayscale_output(self) -> bool:
        opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
        return opt.grayscale or self._bw_toggled

    def _refresh_labels_for_mode(self) -> None:
        opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
        count = len(self._display_items)
        batch_tag = f" ({count}張)" if count > 1 else ""

        if self._model_key == "bw":
            self._title_lbl.setText(f"🖤 黑白完成{batch_tag}！")
            single_save = "💾 儲存黑白 JPEG"
        elif opt.grayscale or self._bw_toggled:
            self._title_lbl.setText(f"🌗 去背黑白{batch_tag}！")
            single_save = "💾 儲存黑白去背圖"
        else:
            self._title_lbl.setText(f"✨ 去背完成{batch_tag}！")
            single_save = "💾 儲存去背圖"

        if count > 1:
            fmt_tag = " JPEG" if self._prefers_jpeg() else ""
            self._save_btn.setText(f"💾 全部儲存{fmt_tag} ({count}張)")
            self._copy_btn.setText(f"📋 複製全部 ({count}張)")
        else:
            self._save_btn.setText(single_save)
            self._copy_btn.setText("📋 複製")

    def _toggle_bw_filter(self) -> None:
        if not self._base_items:
            return
        self._sound.play("click")
        self._bw_toggled = self._bw_btn.isChecked()
        if self._bw_toggled:
            self._display_items = [(convert_to_bw_rgba(img), stem) for img, stem in self._base_items]
        else:
            self._display_items = list(self._base_items)
        self._rebuild_thumbnails()
        self._select_index(self._current_index, play_sound=False)
        self._refresh_labels_for_mode()

    def _select_bg(self, mode: str) -> None:
        self._sound.play("click")
        self._preview.set_bg_mode(mode)
        for m, btn in self._bg_btns.items():
            btn.setChecked(m == mode)

    def _open_model_menu(self) -> None:
        self._sound.play("click")
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #FFF9FC;
                border: 1.5px solid #F5A3C7;
                border-radius: 12px;
                padding: 5px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 12px;
                color: #5C2845;
            }
            QMenu::item {
                padding: 7px 14px;
                border-radius: 8px;
            }
            QMenu::item:selected {
                background-color: #FFE0EE;
                color: #9C1758;
            }
            """
        )
        for key in MODEL_ORDER:
            if key == "bw":
                menu.addSeparator()
            opt = MODEL_OPTIONS[key]
            act = QAction(opt.menu_label, menu)
            act.setCheckable(True)
            act.setChecked(key == self._model_key)
            act.triggered.connect(lambda _c=False, k=key: self._choose_model(k))
            menu.addAction(act)

        pos = self._model_btn.mapToGlobal(QPoint(0, self._model_btn.height() + 4))
        menu.exec(pos)

    def _choose_model(self, key: str) -> None:
        changed = key != self._model_key
        self.set_model_key(key)
        self._sound.play("click")
        self.model_selected.emit(key, changed and bool(self._base_items))

    # --- Save & Copy (Single or Batch) ---
    def save_image(self) -> None:
        if not self._display_items:
            return
        if self._is_batch:
            self._save_all_batch()
        else:
            self.save_current_only()

    def save_current_only(self) -> None:
        if not self._display_items:
            return
        self._sound.play("click")
        pil_img, stem = self._display_items[self._current_index]
        suffix = self._output_suffix()
        prefer_jpg = self._prefers_jpeg()
        default_ext = ".jpg" if prefer_jpg else ".png"
        dlg_title = "儲存星之卡比黑白圖 (JPEG)" if prefer_jpg else "儲存星之卡比去背圖"
        default_path = str(Path(self._last_save_dir) / f"{stem}{suffix}{default_ext}")
        file_filter = (
            "JPEG 圖片 (*.jpg *.jpeg);;PNG 圖片 (*.png)"
            if prefer_jpg
            else "PNG 圖片 (*.png);;JPEG 圖片 (*.jpg *.jpeg)"
        )

        was_top = self._always_on_top
        path, selected_filter = QFileDialog.getSaveFileName(self, dlg_title, default_path, file_filter)
        if was_top:
            self.raise_()
        if not path:
            return

        lower = path.lower()
        if lower.endswith((".jpg", ".jpeg")):
            save_fmt = "JPEG"
        elif lower.endswith(".png"):
            save_fmt = "PNG"
        elif "JPEG" in (selected_filter or "").upper():
            path += ".jpg"
            save_fmt = "JPEG"
        else:
            path += default_ext
            save_fmt = "JPEG" if prefer_jpg else "PNG"

        try:
            save_image_file(pil_img, path, fmt=save_fmt, grayscale=self._is_grayscale_output())
            self._last_save_dir = str(Path(path).parent)
            self._sound.play("save")
            self._show_toast(f"✓ 已儲存：{Path(path).name}")
        except Exception as exc:
            self._show_toast(f"儲存失敗：{exc}", error=True)

    def _save_all_batch(self) -> None:
        self._sound.play("click")
        was_top = self._always_on_top
        folder = QFileDialog.getExistingDirectory(
            self,
            f"選擇要儲存全部 {len(self._display_items)} 張圖片的資料夾",
            self._last_save_dir,
        )
        if was_top:
            self.raise_()
        if not folder:
            return
        dest = Path(folder)
        suffix = self._output_suffix()
        prefer_jpg = self._prefers_jpeg()
        ext = ".jpg" if prefer_jpg else ".png"
        save_fmt = "JPEG" if prefer_jpg else "PNG"
        is_gray = self._is_grayscale_output()
        saved_count = 0
        try:
            for idx, (pil_img, stem) in enumerate(self._display_items, start=1):
                safe = "".join("_" if c in '<>:"/\\|?*' else c for c in (stem or f"kirby_{idx}")).strip(" .")
                target = dest / f"{safe}{suffix}{ext}"
                n = 2
                while target.exists():
                    target = dest / f"{safe}{suffix}_{n}{ext}"
                    n += 1
                save_image_file(pil_img, target, fmt=save_fmt, grayscale=is_gray)
                saved_count += 1
            self._last_save_dir = str(dest)
            self._sound.play("save")
            fmt_label = " JPEG" if prefer_jpg else ""
            self._show_toast(f"✓ 已全部儲存 {saved_count} 張{fmt_label} 至「{dest.name}」！")
        except Exception as exc:
            self._show_toast(f"批次儲存失敗：{exc}", error=True)

    def copy_image(self) -> None:
        if not self._display_items:
            return
        if self._is_batch:
            suffix = self._output_suffix()
            prefer_jpg = self._prefers_jpeg()
            named = [(img, f"{stem}{suffix}") for img, stem in self._display_items]
            paths = copy_multiple_pngs_to_clipboard(
                named,
                fmt="JPEG" if prefer_jpg else "PNG",
                grayscale=self._is_grayscale_output(),
            )
            self._sound.play("save")
            fmt_label = " JPEG" if prefer_jpg else ""
            self._show_toast(f"✓ 已一次複製全部 {len(paths)} 張{fmt_label} 圖片！直接 Ctrl+V 即可貼出")
        else:
            self.copy_current_only()

    def copy_current_only(self) -> None:
        if self._result_pil is None:
            return
        if self._prefers_jpeg():
            _, stem = self._display_items[self._current_index]
            copy_single_jpeg_to_clipboard(
                self._result_pil,
                filename_stem=f"{stem}{self._output_suffix()}",
                grayscale=True,
            )
            self._sound.play("save")
            self._show_toast("✓ 已複製目前這張黑白 JPEG！可直接 Ctrl+V 貼上")
        else:
            copy_transparent_png_to_clipboard(self._result_pil)
            self._sound.play("save")
            self._show_toast("✓ 已複製目前這張透明 PNG！可直接 Ctrl+V 貼上")

    def _on_pick_new(self) -> None:
        self._sound.play("click")
        self.pick_new_requested.emit()

    def _paste_from_clipboard(self) -> None:
        items = read_images_from_clipboard()
        if items:
            self.images_pasted.emit(items)

    def _show_toast(self, text: str, error: bool = False) -> None:
        if error:
            self._toast_lbl.setStyleSheet(
                "color:#9B1C31; background:#FDE8EC; border:1px solid #F5B2C0;"
                "border-radius:9px; padding:4px 8px; font-size:12px; font-weight:700;"
            )
        else:
            self._toast_lbl.setStyleSheet(
                "color:#1F7A4D; background:#E3F9ED; border:1px solid #A8E5C5;"
                "border-radius:9px; padding:4px 8px; font-size:12px; font-weight:700;"
            )
        self._toast_lbl.setText(text)
        self._toast_lbl.setVisible(True)
        self.adjustSize()
        self._toast_timer.start(3200)

    def _clear_toast(self) -> None:
        self._toast_lbl.setVisible(False)
        self.adjustSize()

    # --- Drag card to reposition ---
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()

    # --- Allow dropping single or multiple images straight onto the result card too ---
    def _extract_paths(self, mime) -> List[str]:
        if mime is None or not mime.hasUrls():
            return []
        paths: List[str] = []
        for url in mime.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    paths.append(str(p))
        return paths

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._extract_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._extract_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
            return
        event.ignore()

    def sizeHint(self) -> QSize:
        return QSize(382, 420)
