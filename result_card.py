"""Floating Kirby-styled cutout result preview & save card."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import QPoint, QRect, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
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
    QVBoxLayout,
    QWidget,
)

from bg_worker import MODEL_OPTIONS, MODEL_ORDER, convert_to_bw_rgba
from clipboard_win import (
    IMAGE_EXTENSIONS,
    copy_transparent_png_to_clipboard,
    pil_to_qimage,
    read_image_from_clipboard,
)
from sound_fx import SoundManager


class PreviewCanvas(QWidget):
    """Displays the transparent cutout over checkerboard / light / dark backdrop."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._bg_mode = "checker"  # "checker" | "light" | "dark"
        self.setMinimumSize(316, 236)

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
            # Soft pastel-neutral checkerboard
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
            pad = 14
            avail = rect.adjusted(pad, pad, -pad, -pad)
            scaled = self._pixmap.scaled(
                avail.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            dx = avail.x() + (avail.width() - scaled.width()) // 2
            dy = avail.y() + (avail.height() - scaled.height()) // 2
            p.drawPixmap(dx, dy, scaled)

        # Subtle inner border
        p.setClipping(False)
        p.setPen(QPen(QColor(218, 160, 192, 110), 1.2))
        p.drawRoundedRect(rect, 14, 14)


class ResultCardWindow(QWidget):
    """Frameless rounded floating card showing the cutout emitted from Kirby's back."""

    model_selected = Signal(str, bool)  # (model_key, rerun_current)
    file_dropped = Signal(str)
    image_pasted = Signal(object, str)
    pick_new_requested = Signal()

    def __init__(self, sound: SoundManager, parent=None) -> None:
        super().__init__(parent)
        self._sound = sound
        self._base_pil: Optional[Image.Image] = None
        self._result_pil: Optional[Image.Image] = None
        self._source_stem = "kirby_cutout"
        self._model_key = "fast"
        self._bw_toggled = False
        desktop_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        self._last_save_dir = desktop_dir or str(Path.home() / "Desktop")
        self._drag_offset: Optional[QPoint] = None
        self._always_on_top = True

        self.setWindowTitle("星之卡比去背結果")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setFixedWidth(368)

        self._build_ui()
        self._setup_shortcuts()

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)

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
        layout.setSpacing(10)

        # --- Header (title + top-right model selector + close) ---
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
        self._model_btn.setToolTip("切換去背模型（切換後自動重新去背目前圖片）")
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
        self._bw_btn.setToolTip("一鍵切換彩色／黑白輸出（儲存與複製會套用目前效果）")
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
        self._save_btn.setToolTip("另存為透明背景 PNG（Ctrl+S）")
        self._save_btn.clicked.connect(self.save_image)
        actions.addWidget(self._save_btn, 3)

        self._copy_btn = QPushButton("📋 複製")
        self._copy_btn.setObjectName("SecondaryBtn")
        self._copy_btn.setMinimumHeight(40)
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setToolTip("複製透明去背圖到剪貼簿（Ctrl+C）")
        self._copy_btn.clicked.connect(self.copy_image)
        actions.addWidget(self._copy_btn, 2)

        self._next_btn = QPushButton("🍓 換圖")
        self._next_btn.setObjectName("GhostBtn")
        self._next_btn.setMinimumHeight(40)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setToolTip("再選一張圖片餵給卡比")
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
                font-size: 14px;
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
                font-size: 13px;
                font-weight: 700;
                padding: 0 12px;
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
                font-size: 13px;
                font-weight: 700;
                padding: 0 10px;
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
        self._base_pil = pil_img
        self._result_pil = pil_img
        self._source_stem = source_stem or "kirby_cutout"
        self.set_model_key(model_key)

        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        self._bw_toggled = False
        self._bw_btn.setVisible(not opt.grayscale)
        self._bw_btn.setChecked(False)
        self._refresh_labels_for_mode()

        self._preview.set_image(pil_img)
        self._meta_lbl.setText(f"{elapsed_s:.1f}s" if elapsed_s >= 0.1 else "<0.1s")
        self._dim_lbl.setText(f"{pil_img.width} × {pil_img.height}")
        self._toast_lbl.setVisible(False)
        self.adjustSize()

        # Position cleanly beside Kirby's back (left of Kirby), staying inside monitor bounds
        screen = QApplication.screenAt(kirby_rect.center()) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        card_w = self.width()
        card_h = self.height()

        # Prefer left side (matching Kirby's back pop-out!)
        x = kirby_rect.left() - card_w + 18
        if x < avail.left() + 8:
            # Not enough room on left -> place on right side of Kirby
            x = kirby_rect.right() - 18
        x = max(avail.left() + 8, min(x, avail.right() - card_w - 8))

        y = kirby_rect.center().y() - card_h // 2
        y = max(avail.top() + 8, min(y, avail.bottom() - card_h - 8))

        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def _refresh_labels_for_mode(self) -> None:
        opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
        if self._model_key == "bw":
            self._title_lbl.setText("🖤 黑白轉換完成！")
            self._save_btn.setText("💾 儲存黑白圖")
        elif opt.grayscale or self._bw_toggled:
            self._title_lbl.setText("🌗 去背＋黑白完成！")
            self._save_btn.setText("💾 儲存黑白去背圖")
        else:
            self._title_lbl.setText("✨ 去背完成！")
            self._save_btn.setText("💾 儲存去背圖")

    def _toggle_bw_filter(self) -> None:
        if self._base_pil is None:
            return
        self._sound.play("click")
        self._bw_toggled = self._bw_btn.isChecked()
        self._result_pil = (
            convert_to_bw_rgba(self._base_pil) if self._bw_toggled else self._base_pil
        )
        self._preview.set_image(self._result_pil)
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
        # If changed while a result is open, re-run cutout immediately with the new model!
        self.model_selected.emit(key, changed and self._result_pil is not None)

    def save_image(self) -> None:
        if self._result_pil is None:
            return
        self._sound.play("click")
        opt = MODEL_OPTIONS.get(self._model_key, MODEL_OPTIONS["fast"])
        if self._model_key == "bw":
            suffix = "_bw"
            dlg_title = "儲存星之卡比黑白圖"
        elif opt.grayscale or self._bw_toggled:
            suffix = "_nobg_bw"
            dlg_title = "儲存星之卡比黑白去背圖"
        else:
            suffix = "_nobg"
            dlg_title = "儲存星之卡比去背圖"
        default_name = f"{self._source_stem}{suffix}.png"
        default_path = str(Path(self._last_save_dir) / default_name)

        # Temporarily lower always-on-top so native Save dialog never hides behind card
        was_top = self._always_on_top
        path, _ = QFileDialog.getSaveFileName(
            self,
            dlg_title,
            default_path,
            "PNG 圖片 (*.png)",
        )
        if was_top:
            self.raise_()
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            self._result_pil.save(path, format="PNG")
            self._last_save_dir = str(Path(path).parent)
            self._sound.play("save")
            self._show_toast(f"✓ 已儲存：{Path(path).name}")
        except Exception as exc:
            self._show_toast(f"儲存失敗：{exc}", error=True)

    def copy_image(self) -> None:
        if self._result_pil is None:
            return
        copy_transparent_png_to_clipboard(self._result_pil)
        self._sound.play("save")
        self._show_toast("✓ 已複製透明去背圖！可直接 Ctrl+V 貼上")

    def _on_pick_new(self) -> None:
        self._sound.play("click")
        self.pick_new_requested.emit()

    def _paste_from_clipboard(self) -> None:
        res = read_image_from_clipboard()
        if res is not None:
            img, stem = res
            self.image_pasted.emit(img, stem)

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
        self._toast_timer.start(2800)

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

    # --- Allow dropping another image straight onto the result card too ---
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in IMAGE_EXTENSIONS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.suffix.lower() in IMAGE_EXTENSIONS:
                    event.acceptProposedAction()
                    self.file_dropped.emit(str(p))
                    return
        event.ignore()

    def sizeHint(self) -> QSize:
        return QSize(368, 390)
