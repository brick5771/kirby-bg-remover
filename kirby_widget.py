"""Frameless transparent floating Kirby desktop mascot with 60fps animations."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageOps
from PySide6.QtCore import (
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QConicalGradient,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QWidget,
)

from bg_worker import (
    MODEL_OPTIONS,
    MODEL_ORDER,
    RemoveBgThread,
    SessionCache,
    start_prewarm_daemon,
)
from clipboard_win import IMAGE_EXTENSIONS, pil_to_qimage, qimage_to_pil, read_image_from_clipboard
from result_card import ResultCardWindow
from sound_fx import SoundManager

# Normalized coordinates measured from assets/kirby.png (856 x 858)
MOUTH_NORM_CX = 0.676
MOUTH_NORM_CY = 0.592
MOUTH_NORM_RX = 0.195
MOUTH_NORM_RY = 0.242

BACK_EXIT_NORM_X = 0.14
BACK_EXIT_NORM_Y = 0.20

# Canvas layout constants (logical pixels)
CANVAS_W = 480
CANVAS_H = 350
KIRBY_SIZE = 216
KIRBY_X = 160
KIRBY_Y = 70


@dataclass
class SparkleParticle:
    x: float
    y: float
    vx: float
    vy: float
    size: float
    color: QColor
    age: float = 0.0
    lifetime: float = 0.8
    rotation: float = 0.0
    vrot: float = 180.0
    kind: str = "star"  # "star" | "dot"


@dataclass
class WindStreak:
    angle: float       # radians around mouth (biased toward right/front)
    radius: float      # current distance from mouth center
    max_radius: float
    speed: float
    length: float
    width: float
    alpha: int


def _ease_in_cubic(t: float) -> float:
    return t * t * t


def _ease_out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * ((t - 1.0) ** 3) + c1 * ((t - 1.0) ** 2)


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _draw_four_point_star(p: QPainter, cx: float, cy: float, r: float, rot_deg: float, color: QColor) -> None:
    p.save()
    p.translate(cx, cy)
    p.rotate(rot_deg)
    path = QPainterPath()
    inner = r * 0.34
    for i in range(8):
        ang = i * math.pi / 4.0
        rad = r if i % 2 == 0 else inner
        x = math.cos(ang) * rad
        y = math.sin(ang) * rad
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(path)
    p.restore()


class KirbyMascotWindow(QWidget):
    """Transparent floating Kirby desktop pet & AI background remover."""

    quit_requested = Signal()

    STATE_IDLE = "idle"
    STATE_DRAG_HOVER = "drag_hover"
    STATE_INHALING = "inhaling"
    STATE_DIGESTING = "digesting"
    STATE_POPPING_OUT = "popping_out"

    def __init__(self, assets_dir: Path) -> None:
        super().__init__()
        self._assets_dir = assets_dir
        self._kirby_pixmap = QPixmap(str(assets_dir / "kirby.png"))
        self._kirby_qimage = self._kirby_pixmap.toImage()
        self._icon = QIcon(str(assets_dir / "kirby.ico"))

        # Core services
        self._sound = SoundManager(assets_dir / "sfx", parent=self)
        self._cache = SessionCache()
        self._model_key = "fast"  # Default fastest mode when floating on desktop
        self._always_on_top = True
        desktop_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        self._last_open_dir = desktop_dir or str(Path.home() / "Desktop")

        # State machine
        self._state = self.STATE_IDLE
        self._state_t = 0.0
        self._clock_t = 0.0
        self._last_tick = time.perf_counter()
        self._status_text = ""

        # Current job data
        self._current_source_pil: Optional[Image.Image] = None
        self._current_source_stem: str = "kirby_cutout"
        self._inhale_pixmap: Optional[QPixmap] = None
        self._popout_pixmap: Optional[QPixmap] = None
        self._pending_result: Optional[tuple[Image.Image, float, str, str]] = None
        self._worker: Optional[RemoveBgThread] = None
        self._inhale_duration = 0.96
        self._min_digest_duration = 0.42
        self._popout_duration = 0.88

        # Visual particles
        self._particles: List[SparkleParticle] = []
        self._wind_streaks: List[WindStreak] = []
        self._init_wind_streaks()

        # Interaction tracking
        self._dragging_window = False
        self._press_global: Optional[QPoint] = None
        self._press_local: Optional[QPointF] = None
        self._win_origin: Optional[QPoint] = None
        self._hovering = False
        self._hover_mouth = False
        self._menu_open = False

        # Floating Result Card
        self._result_card = ResultCardWindow(self._sound)
        self._result_card.setWindowIcon(self._icon)
        self._result_card.model_selected.connect(self._on_model_selected)
        self._result_card.file_dropped.connect(self.eat_file)
        self._result_card.image_pasted.connect(self.eat_pil_image)
        self._result_card.pick_new_requested.connect(self.open_file_picker)

        # Window setup: frameless, transparent, stays on top
        self.setWindowTitle("星之卡比去背精靈")
        self.setWindowIcon(self._icon)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFixedSize(CANVAS_W, CANVAS_H)

        self._build_hover_toolbar()
        self._setup_shortcuts()
        self._setup_tray()
        self._position_initial()

        # Fade-hide timer for hover controls
        self._toolbar_hide_timer = QTimer(self)
        self._toolbar_hide_timer.setSingleShot(True)
        self._toolbar_hide_timer.timeout.connect(self._maybe_hide_toolbar)

        # 60fps animation loop
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(16)

        # Prewarm fastest model on a safe daemon thread right after UI appears
        self._prewarm = start_prewarm_daemon(self._cache, "fast")

    def _position_initial(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        # Place Kirby comfortably at lower-right quadrant of desktop
        x = avail.right() - CANVAS_W - 28
        y = avail.bottom() - CANVAS_H - 36
        self.move(x, y)

    def _build_hover_toolbar(self) -> None:
        """Compact floating pill bar at Kirby's top-right (visible on hover)."""
        self._toolbar = QWidget(self)
        self._toolbar.setObjectName("HoverBar")
        row = QHBoxLayout(self._toolbar)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(5)

        self._model_btn = QPushButton("⚡ 快速 ▾")
        self._model_btn.setObjectName("TopModelBtn")
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.setToolTip("選擇去背或黑白轉換模式（預設快速去背）")
        self._model_btn.clicked.connect(self._open_model_menu)
        row.addWidget(self._model_btn)

        self._mute_btn = QPushButton("🔊")
        self._mute_btn.setObjectName("MiniIconBtn")
        self._mute_btn.setFixedSize(26, 26)
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.setToolTip("開關音效")
        self._mute_btn.clicked.connect(self._toggle_mute)
        row.addWidget(self._mute_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("MiniCloseBtn")
        self._close_btn.setFixedSize(26, 26)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("關閉程式")
        self._close_btn.clicked.connect(self.quit_app)
        row.addWidget(self._close_btn)

        self._toolbar.setStyleSheet(
            """
            QWidget#HoverBar {
                background-color: rgba(255, 247, 252, 236);
                border: 1.5px solid #F6A2C7;
                border-radius: 17px;
            }
            QPushButton#TopModelBtn {
                background-color: #FFE9F4;
                color: #A11B5E;
                border: 1.5px solid #F29BC2;
                border-radius: 11px;
                min-height: 22px;
                padding: 1px 10px;
                font-family: "Microsoft JhengHei UI", "Segoe UI";
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton#TopModelBtn:hover {
                background-color: #FFD6EA;
            }
            QPushButton#MiniIconBtn {
                background-color: #FFF3F9;
                color: #9C2E66;
                border: 1px solid #F2BDD6;
                border-radius: 13px;
                font-size: 12px;
            }
            QPushButton#MiniIconBtn:hover {
                background-color: #FFE0EE;
            }
            QPushButton#MiniCloseBtn {
                background-color: #FFF3F9;
                color: #B4537B;
                border: 1px solid #F2BDD6;
                border-radius: 13px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#MiniCloseBtn:hover {
                background-color: #FF5E99;
                color: #FFFFFF;
                border-color: #E0447E;
            }
            """
        )
        self._toolbar.adjustSize()
        self._place_toolbar()
        self._toolbar.setVisible(False)

    def _place_toolbar(self) -> None:
        self._toolbar.adjustSize()
        tx = min(CANVAS_W - self._toolbar.width() - 8, KIRBY_X + KIRBY_SIZE - self._toolbar.width() + 42)
        ty = max(6, KIRBY_Y - 40)
        self._toolbar.move(tx, ty)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self.paste_from_clipboard)

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._icon, self)
        self._tray.setToolTip("星之卡比去背精靈（拖曳圖片進嘴巴即可去背）")

        menu = QMenu()
        act_show = QAction("🌸 顯示星之卡比", menu)
        act_show.triggered.connect(self.bring_to_front)
        menu.addAction(act_show)

        act_pick = QAction("🍓 選擇圖片去背...", menu)
        act_pick.triggered.connect(self.open_file_picker)
        menu.addAction(act_pick)

        act_paste = QAction("📋 貼上剪貼簿圖片 (Ctrl+V)", menu)
        act_paste.triggered.connect(self.paste_from_clipboard)
        menu.addAction(act_paste)

        menu.addSeparator()
        act_quit = QAction("✕ 結束程式", menu)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.bring_to_front()

    def bring_to_front(self) -> None:
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._anim_timer.stop()
        self._toolbar_hide_timer.stop()
        self._tray.hide()
        self._result_card.close()
        self.close()
        self.quit_requested.emit()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        self._anim_timer.stop()
        self._toolbar_hide_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(5000)
        super().closeEvent(event)

    # --- Geometry helpers ---
    def _kirby_rect(self) -> QRectF:
        return QRectF(KIRBY_X, KIRBY_Y, KIRBY_SIZE, KIRBY_SIZE)

    def _mouth_center(self) -> QPointF:
        return QPointF(
            KIRBY_X + KIRBY_SIZE * MOUTH_NORM_CX,
            KIRBY_Y + KIRBY_SIZE * MOUTH_NORM_CY,
        )

    def _mouth_radii(self) -> tuple[float, float]:
        return (KIRBY_SIZE * MOUTH_NORM_RX, KIRBY_SIZE * MOUTH_NORM_RY)

    def _back_exit_point(self) -> QPointF:
        return QPointF(
            KIRBY_X + KIRBY_SIZE * BACK_EXIT_NORM_X,
            KIRBY_Y + KIRBY_SIZE * BACK_EXIT_NORM_Y,
        )

    def _is_point_in_mouth(self, pt: QPointF) -> bool:
        mc = self._mouth_center()
        rx, ry = self._mouth_radii()
        dx = (pt.x() - mc.x()) / max(rx, 1.0)
        dy = (pt.y() - mc.y()) / max(ry, 1.0)
        return (dx * dx + dy * dy) <= 1.08

    def _is_point_on_kirby(self, pt: QPointF) -> bool:
        r = self._kirby_rect()
        if not r.contains(pt):
            return False
        # Check cached alpha on Kirby sprite for fast silhouette hit-test
        nx = (pt.x() - r.x()) / r.width()
        ny = (pt.y() - r.y()) / r.height()
        img = self._kirby_qimage
        px = max(0, min(img.width() - 1, int(nx * img.width())))
        py = max(0, min(img.height() - 1, int(ny * img.height())))
        return img.pixelColor(px, py).alpha() > 25

    # --- Particles & Wind ---
    def _init_wind_streaks(self) -> None:
        self._wind_streaks.clear()
        for i in range(9):
            ang = math.radians(-52 + i * 14 + random.uniform(-4, 4))
            max_r = random.uniform(118.0, 162.0)
            self._wind_streaks.append(
                WindStreak(
                    angle=ang,
                    radius=random.uniform(42.0, max_r),
                    max_radius=max_r,
                    speed=random.uniform(215.0, 325.0),
                    length=random.uniform(32.0, 56.0),
                    width=random.uniform(2.5, 4.2),
                    alpha=random.randint(160, 235),
                )
            )

    def _spawn_back_burst(self) -> None:
        origin = self._back_exit_point()
        palette = [
            QColor("#FF76B8"),
            QColor("#FFD659"),
            QColor("#7CE4FF"),
            QColor("#FFB6DB"),
            QColor("#FFFFFF"),
            QColor("#C69BFF"),
        ]
        for _ in range(18):
            ang = math.radians(random.uniform(118, 252))
            spd = random.uniform(75.0, 215.0)
            self._particles.append(
                SparkleParticle(
                    x=origin.x() + random.uniform(-10, 10),
                    y=origin.y() + random.uniform(-10, 10),
                    vx=math.cos(ang) * spd,
                    vy=math.sin(ang) * spd,
                    size=random.uniform(6.0, 14.5),
                    color=random.choice(palette),
                    lifetime=random.uniform(0.55, 1.05),
                    rotation=random.uniform(0, 360),
                    vrot=random.uniform(-240, 240),
                    kind="star" if random.random() < 0.72 else "dot",
                )
            )

    def _spawn_inhale_sparkle(self) -> None:
        mc = self._mouth_center()
        ang = math.radians(random.uniform(-55, 65))
        dist = random.uniform(95.0, 145.0)
        x = mc.x() + math.cos(ang) * dist
        y = mc.y() + math.sin(ang) * dist
        speed = random.uniform(190.0, 260.0)
        self._particles.append(
            SparkleParticle(
                x=x,
                y=y,
                vx=-math.cos(ang) * speed,
                vy=-math.sin(ang) * speed,
                size=random.uniform(5.0, 9.5),
                color=random.choice([QColor("#FFE57F"), QColor("#FFC4E1"), QColor("#FFFFFF")]),
                lifetime=0.42,
                rotation=random.uniform(0, 360),
                vrot=300.0,
                kind="star",
            )
        )

    # --- Public actions ---
    def open_file_picker(self) -> None:
        if self._state in (self.STATE_INHALING, self.STATE_DIGESTING):
            return
        self._sound.play("click")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "選擇要餵給星之卡比去背的圖片",
            self._last_open_dir,
            "圖片檔案 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff);;所有檔案 (*.*)",
        )
        if path:
            self._last_open_dir = str(Path(path).parent)
            self.eat_file(path)

    def paste_from_clipboard(self) -> None:
        if self._state in (self.STATE_INHALING, self.STATE_DIGESTING):
            return
        res = read_image_from_clipboard()
        if res is None:
            self._show_temp_status("剪貼簿裡沒有圖片喔！")
            return
        img, stem = res
        self.eat_pil_image(img, stem)

    def eat_file(self, path_str: str) -> None:
        p = Path(path_str)
        if not p.is_file():
            return
        try:
            img = Image.open(p)
            img = ImageOps.exif_transpose(img).convert("RGBA")
        except Exception as exc:
            self._show_temp_status(f"無法讀取圖片：{exc}")
            return
        self._last_open_dir = str(p.parent)
        self.eat_pil_image(img, p.stem)

    def eat_pil_image(self, pil_img: Image.Image, source_stem: str = "kirby_cutout") -> None:
        # Cancel/ignore if already eating
        if self._worker is not None and self._worker.isRunning():
            return

        self._current_source_pil = pil_img.copy()
        self._current_source_stem = source_stem or "kirby_cutout"
        self._pending_result = None

        # Create crisp thumbnail for the spiraling inhale animation
        thumb = pil_img.copy()
        thumb.thumbnail((112, 112), Image.Resampling.LANCZOS)
        self._inhale_pixmap = QPixmap.fromImage(pil_to_qimage(thumb))

        # Hide previous result card while Kirby eats the new image
        self._result_card.hide()

        # Enter INHALING animation AND start rembg worker immediately in parallel!
        self._state = self.STATE_INHALING
        self._state_t = 0.0
        self._status_text = ""
        self._sound.play("inhale")
        self._start_worker(self._current_source_pil, self._model_key, self._current_source_stem)
        self.update()

    def _rerun_current_with_model(self, model_key: str) -> None:
        if self._current_source_pil is None:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._pending_result = None
        self._result_card.hide()
        self._state = self.STATE_DIGESTING
        self._state_t = 0.0
        self._status_text = f"切換 {MODEL_OPTIONS[model_key].short_label} 重新處理中..."
        self._sound.play("gulp")
        self._start_worker(self._current_source_pil, model_key, self._current_source_stem)
        self.update()

    def _start_worker(self, pil_img: Image.Image, model_key: str, stem: str) -> None:
        worker = RemoveBgThread(self._cache, pil_img, model_key, stem, parent=self)
        worker.status_changed.connect(self._on_worker_status)
        worker.finished_ok.connect(self._on_worker_ok)
        worker.finished_err.connect(self._on_worker_err)
        worker.finished.connect(self._on_worker_cleanup)
        self._worker = worker
        worker.start()

    def _on_worker_status(self, msg: str) -> None:
        self._status_text = msg

    def _on_worker_ok(self, result_pil: Image.Image, elapsed_s: float, model_key: str, stem: str) -> None:
        self._pending_result = (result_pil, elapsed_s, model_key, stem)
        thumb = result_pil.copy()
        thumb.thumbnail((132, 132), Image.Resampling.LANCZOS)
        self._popout_pixmap = QPixmap.fromImage(pil_to_qimage(thumb))

    def _on_worker_err(self, err_msg: str) -> None:
        self._state = self.STATE_IDLE
        self._state_t = 0.0
        self._show_temp_status(err_msg)

    def _on_worker_cleanup(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _show_temp_status(self, msg: str) -> None:
        self._status_text = msg
        self.update()
        QTimer.singleShot(2600, self._clear_temp_status)

    def _clear_temp_status(self) -> None:
        if self._state == self.STATE_IDLE:
            self._status_text = ""
            self.update()

    # --- Controls & Menus ---
    def _on_model_selected(self, model_key: str, rerun_current: bool) -> None:
        self._model_key = model_key
        opt = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS["fast"])
        self._model_btn.setText(f"{opt.short_label} ▾")
        self._result_card.set_model_key(model_key)
        self._place_toolbar()
        if rerun_current and self._current_source_pil is not None:
            self._rerun_current_with_model(model_key)

    def _open_model_menu(self) -> None:
        self._sound.play("click")
        self._menu_open = True
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
            act.triggered.connect(
                lambda _c=False, k=key: self._on_model_selected(
                    k, self._result_card.isVisible() and k != self._model_key
                )
            )
            menu.addAction(act)

        pos = self._model_btn.mapToGlobal(QPoint(0, self._model_btn.height() + 4))
        menu.exec(pos)
        self._menu_open = False
        self._toolbar_hide_timer.start(600)

    def _toggle_mute(self) -> None:
        muted = self._sound.toggle_mute()
        self._mute_btn.setText("🔇" if muted else "🔊")
        if not muted:
            self._sound.play("click")

    def _toggle_always_on_top(self) -> None:
        self._always_on_top = not self._always_on_top
        flags = Qt.WindowType.FramelessWindowHint
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        pos = self.pos()
        self.setWindowFlags(flags)
        self.move(pos)
        self.show()
        self._result_card.set_always_on_top(self._always_on_top)

    def _open_context_menu(self, global_pos: QPoint) -> None:
        self._menu_open = True
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

        act_pick = QAction("🍓 選擇圖片餵給卡比...", menu)
        act_pick.triggered.connect(self.open_file_picker)
        menu.addAction(act_pick)

        act_paste = QAction("📋 貼上剪貼簿圖片 (Ctrl+V)", menu)
        act_paste.triggered.connect(self.paste_from_clipboard)
        menu.addAction(act_paste)

        menu.addSeparator()
        model_sub = menu.addMenu("🎛️ 切換去背／黑白模式")
        model_sub.setStyleSheet(menu.styleSheet())
        for key in MODEL_ORDER:
            if key == "bw":
                model_sub.addSeparator()
            opt = MODEL_OPTIONS[key]
            act = QAction(opt.menu_label, model_sub)
            act.setCheckable(True)
            act.setChecked(key == self._model_key)
            act.triggered.connect(
                lambda _c=False, k=key: self._on_model_selected(
                    k, self._result_card.isVisible() and k != self._model_key
                )
            )
            model_sub.addAction(act)

        act_mute = QAction("🔊 音效已開啟" if not self._sound.muted else "🔇 音效已靜音", menu)
        act_mute.triggered.connect(self._toggle_mute)
        menu.addAction(act_mute)

        act_top = QAction("📌 永遠顯示在最上層", menu)
        act_top.setCheckable(True)
        act_top.setChecked(self._always_on_top)
        act_top.triggered.connect(self._toggle_always_on_top)
        menu.addAction(act_top)

        menu.addSeparator()
        act_quit = QAction("✕ 結束星之卡比去背精靈", menu)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)

        menu.exec(global_pos)
        self._menu_open = False
        self._toolbar_hide_timer.start(600)

    def _show_toolbar(self) -> None:
        self._toolbar_hide_timer.stop()
        if not self._toolbar.isVisible():
            self._place_toolbar()
            self._toolbar.setVisible(True)

    def _maybe_hide_toolbar(self) -> None:
        if self._menu_open:
            return
        cursor_local = self.mapFromGlobal(self.cursor().pos())
        over_bar = self._toolbar.isVisible() and self._toolbar.geometry().adjusted(-8, -8, 8, 8).contains(cursor_local)
        over_kirby = self._is_point_on_kirby(QPointF(cursor_local))
        if not over_bar and not over_kirby:
            self._hovering = False
            self._hover_mouth = False
            self._toolbar.setVisible(False)
            self.update()

    # --- 60fps Simulation Tick ---
    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now
        self._clock_t += dt
        self._state_t += dt

        # Update wind streaks during drag-hover and inhaling
        if self._state in (self.STATE_DRAG_HOVER, self.STATE_INHALING):
            speed_mult = 1.65 if self._state == self.STATE_INHALING else 1.0
            for ws in self._wind_streaks:
                ws.radius -= ws.speed * speed_mult * dt
                if ws.radius < 18.0:
                    ws.radius = ws.max_radius
                    ws.angle = math.radians(random.uniform(-56, 62))
            if self._state == self.STATE_INHALING and random.random() < 0.35:
                self._spawn_inhale_sparkle()

        # Update sparkles
        alive: List[SparkleParticle] = []
        for sp in self._particles:
            sp.age += dt
            if sp.age < sp.lifetime:
                sp.x += sp.vx * dt
                sp.y += sp.vy * dt
                sp.vx *= 0.96
                sp.vy *= 0.96
                sp.rotation += sp.vrot * dt
                alive.append(sp)
        self._particles = alive

        # State transitions
        if self._state == self.STATE_INHALING:
            if self._state_t >= self._inhale_duration:
                self._state = self.STATE_DIGESTING
                self._state_t = 0.0
                self._sound.play("gulp")
        elif self._state == self.STATE_DIGESTING:
            if self._pending_result is not None and self._state_t >= self._min_digest_duration:
                self._state = self.STATE_POPPING_OUT
                self._state_t = 0.0
                self._status_text = ""
                self._sound.play("popout")
                self._spawn_back_burst()
        elif self._state == self.STATE_POPPING_OUT:
            if self._state_t >= self._popout_duration:
                self._state = self.STATE_IDLE
                self._state_t = 0.0
                if self._pending_result is not None:
                    res_img, elapsed_s, model_key, stem = self._pending_result
                    self._pending_result = None
                    kirby_global_rect = QRect(
                        self.mapToGlobal(QPoint(KIRBY_X, KIRBY_Y)),
                        self._kirby_rect().toRect().size(),
                    )
                    self._result_card.show_result(
                        res_img, elapsed_s, model_key, stem, kirby_global_rect
                    )

        self.update()

    # --- Custom 60fps QPainter Rendering ---
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        mc = self._mouth_center()
        kr = self._kirby_rect()
        kc = kr.center()

        # Compute Kirby squash/stretch/float transform for current state
        bob_y = 0.0
        sx = 1.0
        sy = 1.0
        rot_deg = 0.0
        jitter_x = 0.0
        jitter_y = 0.0

        if self._state == self.STATE_IDLE:
            bob_y = math.sin(self._clock_t * 2.3) * 4.2
            breath = math.sin(self._clock_t * 2.3 + 0.6) * 0.014
            sx = 1.0 + breath
            sy = 1.0 - breath
            if self._hover_mouth:
                sx *= 1.03
                sy *= 1.03
        elif self._state == self.STATE_DRAG_HOVER:
            bob_y = math.sin(self._clock_t * 10.0) * 2.0
            jitter_x = math.sin(self._clock_t * 46.0) * 1.8
            jitter_y = math.cos(self._clock_t * 53.0) * 1.4
            sx = 1.055
            sy = 1.045
            rot_deg = 3.0
        elif self._state == self.STATE_INHALING:
            prog = min(1.0, self._state_t / self._inhale_duration)
            jitter_x = math.sin(self._clock_t * 55.0) * (2.2 * (1.0 - 0.4 * prog))
            jitter_y = math.cos(self._clock_t * 62.0) * 1.6
            swell = 0.04 + 0.07 * math.sin(prog * math.pi)
            sx = 1.0 + swell
            sy = 1.0 + swell * 0.75
            rot_deg = -4.0 * math.sin(prog * math.pi)
        elif self._state == self.STATE_DIGESTING:
            wobble = math.sin(self._state_t * 13.5)
            sx = 1.11 + 0.045 * wobble
            sy = 1.11 - 0.045 * wobble
            rot_deg = math.sin(self._state_t * 6.8) * 5.0
            bob_y = -abs(math.sin(self._state_t * 6.8)) * 6.5
        elif self._state == self.STATE_POPPING_OUT:
            prog = min(1.0, self._state_t / self._popout_duration)
            # Elastic recoil pop!
            recoil = math.exp(-prog * 6.5) * math.cos(prog * 15.0)
            sx = 1.0 + 0.13 * recoil
            sy = 1.0 - 0.11 * recoil
            rot_deg = 6.5 * math.exp(-prog * 5.0)

        # 0) Soft ground shadow beneath Kirby
        p.save()
        shadow_w = 132.0 * sx * (1.0 - bob_y * 0.012)
        shadow_h = 22.0
        shadow_cx = kc.x() + jitter_x
        shadow_cy = kr.bottom() + 4.0
        grad = QRadialGradient(shadow_cx, shadow_cy, shadow_w * 0.55)
        grad.setColorAt(0.0, QColor(140, 42, 92, 58))
        grad.setColorAt(1.0, QColor(140, 42, 92, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(shadow_cx, shadow_cy), shadow_w * 0.5, shadow_h * 0.5)
        p.restore()

        # 1) During DRAG_HOVER, paint subtle translucent suction aura around mouth
        #    so the front drop zone stays softly interactive & visually inviting
        if self._state == self.STATE_DRAG_HOVER:
            self._paint_suction_aura(p, mc)

        # 2) POPPING_OUT Phase A (t < 0.48): draw cutout BEHIND Kirby's back!
        pop_prog = min(1.0, self._state_t / self._popout_duration) if self._state == self.STATE_POPPING_OUT else 0.0
        if self._state == self.STATE_POPPING_OUT and pop_prog < 0.48:
            self._paint_popout_cutout(p, pop_prog)

        # 3) Draw Kirby sprite with elastic squash/stretch & float
        p.save()
        p.translate(kc.x() + jitter_x, kc.y() + bob_y + jitter_y)
        p.rotate(rot_deg)
        p.scale(sx, sy)
        target = QRectF(-KIRBY_SIZE / 2.0, -KIRBY_SIZE / 2.0, KIRBY_SIZE, KIRBY_SIZE)
        p.drawPixmap(target, self._kirby_pixmap, QRectF(self._kirby_pixmap.rect()))
        p.restore()

        # 4) Suction wind lines & swirling incoming image into Kirby's mouth
        if self._state in (self.STATE_DRAG_HOVER, self.STATE_INHALING):
            self._paint_wind_streaks(p, QPointF(mc.x() + jitter_x, mc.y() + bob_y + jitter_y))

        if self._state == self.STATE_INHALING and self._inhale_pixmap is not None:
            self._paint_inhaling_image(p, QPointF(mc.x() + jitter_x, mc.y() + bob_y + jitter_y))

        # 5) POPPING_OUT Phase B (t >= 0.48): cutout leaps clear in front of Kirby's back!
        if self._state == self.STATE_POPPING_OUT and pop_prog >= 0.48:
            self._paint_popout_cutout(p, pop_prog)

        # 6) Orbiting stars while digesting
        if self._state == self.STATE_DIGESTING:
            self._paint_digest_orbit(p, QPointF(kc.x(), kc.y() + bob_y))

        # 7) Sparkle burst particles
        for sp in self._particles:
            alpha = int(255 * max(0.0, 1.0 - (sp.age / sp.lifetime) ** 1.4))
            c = QColor(sp.color)
            c.setAlpha(alpha)
            if sp.kind == "star":
                _draw_four_point_star(p, sp.x, sp.y, sp.size * (1.0 - 0.35 * sp.age / sp.lifetime), sp.rotation, c)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                rad = sp.size * 0.35
                p.drawEllipse(QPointF(sp.x, sp.y), rad, rad)

        # 8) Contextual status / hover hint pill below Kirby
        pill_text = ""
        if self._state == self.STATE_DRAG_HOVER:
            if self._model_key == "bw":
                pill_text = "放開餵進卡比嘴巴轉黑白！"
            elif self._model_key == "fast_bw":
                pill_text = "放開餵進嘴巴去背＋轉黑白！"
            else:
                pill_text = "放開餵進卡比嘴巴去背！"
        elif self._state == self.STATE_DIGESTING:
            pill_text = self._status_text or (
                "卡比轉黑白中..." if self._model_key == "bw" else "卡比消化去背中..."
            )
        elif self._status_text:
            pill_text = self._status_text
        elif self._state == self.STATE_IDLE and self._hovering:
            pill_text = "拖圖進嘴巴・點嘴巴選圖・Ctrl+V"

        if pill_text:
            self._paint_bottom_pill(p, kc.x(), kr.bottom() + 26.0, pill_text)

    def _paint_suction_aura(self, p: QPainter, mc: QPointF) -> None:
        p.save()
        pulse = 1.0 + 0.08 * math.sin(self._clock_t * 10.0)
        aura_r = 128.0 * pulse
        grad = QRadialGradient(mc.x() + 22, mc.y() + 6, aura_r)
        grad.setColorAt(0.0, QColor(255, 130, 195, 78))
        grad.setColorAt(0.55, QColor(255, 185, 224, 34))
        grad.setColorAt(1.0, QColor(255, 185, 224, 10))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(mc.x() + 22, mc.y() + 6), aura_r, aura_r * 0.88)

        # Dashed cute target ring around mouth
        rx, ry = self._mouth_radii()
        pen = QPen(QColor(255, 240, 248, 200), 2.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(-self._clock_t * 14.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(mc, rx * 1.05 * pulse, ry * 1.05 * pulse)
        p.restore()

    def _paint_wind_streaks(self, p: QPainter, mc: QPointF) -> None:
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        for ws in self._wind_streaks:
            r_in = max(8.0, ws.radius - ws.length)
            r_out = ws.radius
            # Slight swirl curve as wind enters mouth
            swirl_in = ws.angle + 0.16 * (1.0 - r_in / ws.max_radius)
            swirl_mid = ws.angle + 0.08 * (1.0 - (r_in + r_out) * 0.5 / ws.max_radius)
            p0 = QPointF(mc.x() + math.cos(ws.angle) * r_out, mc.y() + math.sin(ws.angle) * r_out)
            pm = QPointF(
                mc.x() + math.cos(swirl_mid) * (r_in + r_out) * 0.5,
                mc.y() + math.sin(swirl_mid) * (r_in + r_out) * 0.5,
            )
            p1 = QPointF(mc.x() + math.cos(swirl_in) * r_in, mc.y() + math.sin(swirl_in) * r_in)

            fade = min(1.0, (ws.max_radius - ws.radius) / 28.0) * min(1.0, ws.radius / 38.0)
            alpha = int(ws.alpha * max(0.0, fade))
            pen = QPen(QColor(255, 250, 255, alpha), ws.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            path = QPainterPath(p0)
            path.quadTo(pm, p1)
            p.drawPath(path)
        p.restore()

    def _paint_inhaling_image(self, p: QPainter, mc: QPointF) -> None:
        """Rotates and shrinks the user's image along a vortex spiral straight into Kirby's mouth."""
        prog = min(1.0, self._state_t / self._inhale_duration)
        pm = self._inhale_pixmap
        if pm is None:
            return

        p.save()

        # Soft mouth-cavity clipping during the final swallow stage so image vanishes INSIDE mouth
        if prog > 0.56:
            clip_blend = min(1.0, (prog - 0.56) / 0.24)
            rx, ry = self._mouth_radii()
            # Interpolate from generous oval down to exact mouth cavity oval
            cur_rx = rx * (2.6 - 1.56 * clip_blend)
            cur_ry = ry * (2.6 - 1.56 * clip_blend)
            clip_path = QPainterPath()
            clip_path.addEllipse(mc, cur_rx, cur_ry)
            p.setClipPath(clip_path)

        # Spiral trajectory: starts in front-right of Kirby's mouth and swirls tightly to mouth center
        start_radius = 102.0
        radius = start_radius * (1.0 - (prog ** 1.32))
        # Clockwise spiral angle stays cleanly clear of Kirby's eyes and inside canvas
        orbit_angle = math.radians(16.0) + (prog ** 1.38) * math.radians(330.0)
        ix = mc.x() + math.cos(orbit_angle) * radius
        iy = mc.y() + math.sin(orbit_angle) * radius * 0.68

        # Image self-rotation (spins faster and faster as it gets sucked in!)
        spin_deg = -(prog ** 1.55) * 900.0

        # Smooth shrinking down to tiny point inside throat
        scale = max(0.04, 1.0 - _ease_in_cubic(prog) * 0.96)
        opacity = 1.0 if prog < 0.80 else max(0.0, 1.0 - (prog - 0.80) / 0.20)

        p.setOpacity(opacity)
        p.translate(ix, iy)
        p.rotate(spin_deg)
        p.scale(scale, scale)

        # Cute subtle white sticker border around incoming image so it pops against desktop
        bw, bh = pm.width(), pm.height()
        frame = QRectF(-bw / 2.0, -bh / 2.0, bw, bh)
        p.setPen(QPen(QColor(255, 255, 255, 210), 3.0))
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(frame.adjusted(-4, -4, 4, 4), 10, 10)
        p.drawPixmap(frame, pm, QRectF(pm.rect()))

        # Subtle mouth-depth shadow as it enters the dark throat
        if prog > 0.65:
            dark_alpha = int(150 * ((prog - 0.65) / 0.35))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(115, 16, 46, dark_alpha))
            p.drawRoundedRect(frame.adjusted(-4, -4, 4, 4), 10, 10)

        p.restore()

    def _paint_popout_cutout(self, p: QPainter, prog: float) -> None:
        """Emits the background-removed cutout in a playful arc from behind Kirby's back."""
        pm = self._popout_pixmap
        if pm is None:
            return

        # Cubic Bezier arc starting hidden behind Kirby's back -> curving out upper-left -> landing left of back
        p0 = QPointF(KIRBY_X + KIRBY_SIZE * 0.42, KIRBY_Y + KIRBY_SIZE * 0.44)
        p1 = QPointF(KIRBY_X + KIRBY_SIZE * 0.12, KIRBY_Y + KIRBY_SIZE * 0.02)
        p2 = QPointF(KIRBY_X - 52.0, KIRBY_Y - 10.0)
        p3 = QPointF(KIRBY_X - 76.0, KIRBY_Y + KIRBY_SIZE * 0.44)

        t = _ease_out_cubic(prog)
        u = 1.0 - t
        bx = (u**3) * p0.x() + 3 * (u**2) * t * p1.x() + 3 * u * (t**2) * p2.x() + (t**3) * p3.x()
        by = (u**3) * p0.y() + 3 * (u**2) * t * p1.y() + 3 * u * (t**2) * p2.y() + (t**3) * p3.y()

        scale = 0.16 + 0.84 * min(1.12, _ease_out_back(min(1.0, prog * 1.05)))
        rot_deg = -32.0 * (1.0 - t) + math.sin(prog * math.pi * 2.0) * 6.0 * (1.0 - prog)
        opacity = min(1.0, prog / 0.18)

        p.save()
        p.setOpacity(opacity)
        p.translate(bx, by)
        p.rotate(rot_deg)
        p.scale(scale, scale)

        # Glowing starlight halo behind the freshly cut-out transparent PNG!
        halo_r = max(pm.width(), pm.height()) * 0.62
        grad = QRadialGradient(0, 0, halo_r)
        grad.setColorAt(0.0, QColor(255, 232, 145, 165))
        grad.setColorAt(0.55, QColor(255, 152, 208, 95))
        grad.setColorAt(1.0, QColor(255, 152, 208, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(0, 0), halo_r, halo_r)

        rect = QRectF(-pm.width() / 2.0, -pm.height() / 2.0, pm.width(), pm.height())
        p.drawPixmap(rect, pm, QRectF(pm.rect()))
        p.restore()

    def _paint_digest_orbit(self, p: QPainter, center: QPointF) -> None:
        colors = [QColor("#FFD659"), QColor("#FF85C0"), QColor("#8CE5FF"), QColor("#FFFFFF")]
        for i in range(4):
            ang = self._state_t * 4.2 + i * (math.pi / 2.0)
            x = center.x() + math.cos(ang) * 116.0
            y = center.y() + math.sin(ang) * 96.0
            _draw_four_point_star(p, x, y, 8.5, self._state_t * 180.0 + i * 45.0, colors[i])

    def _paint_bottom_pill(self, p: QPainter, cx: float, cy: float, text: str) -> None:
        p.save()
        font = QFont("Microsoft JhengHei UI", 9)
        font.setBold(True)
        p.setFont(font)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        pad_x, pad_y = 12, 5
        rect = QRectF(cx - tw / 2.0 - pad_x, cy - th / 2.0 - pad_y, tw + pad_x * 2, th + pad_y * 2)

        p.setPen(QPen(QColor("#F5A3C7"), 1.4))
        p.setBrush(QColor(255, 248, 252, 236))
        p.drawRoundedRect(rect, 13, 13)

        p.setPen(QColor("#8E1E55"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    # --- Mouse & Hover Events ---
    def mousePressEvent(self, event: QMouseEvent) -> None:
        pt = event.position()
        if event.button() == Qt.MouseButton.RightButton:
            if self._is_point_on_kirby(pt):
                self._open_context_menu(event.globalPosition().toPoint())
                event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._is_point_on_kirby(pt):
            self._press_local = pt
            self._press_global = event.globalPosition().toPoint()
            self._win_origin = self.frameGeometry().topLeft()
            self._dragging_window = False
            self.activateWindow()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pt = event.position()
        if self._press_global is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            delta = event.globalPosition().toPoint() - self._press_global
            if not self._dragging_window and delta.manhattanLength() > 5:
                self._dragging_window = True
            if self._dragging_window and self._win_origin is not None:
                self.move(self._win_origin + delta)
                event.accept()
                return

        on_kirby = self._is_point_on_kirby(pt)
        in_mouth = on_kirby and self._is_point_in_mouth(pt)
        if on_kirby != self._hovering or in_mouth != self._hover_mouth:
            self._hovering = on_kirby
            self._hover_mouth = in_mouth
            if on_kirby:
                self._show_toolbar()
                self.setCursor(
                    Qt.CursorShape.PointingHandCursor
                    if in_mouth
                    else Qt.CursorShape.OpenHandCursor
                )
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self._toolbar_hide_timer.start(520)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_click = (
                self._press_local is not None
                and not self._dragging_window
                and self._is_point_in_mouth(event.position())
            )
            self._press_global = None
            self._press_local = None
            self._win_origin = None
            self._dragging_window = False
            if was_click:
                self.open_file_picker()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._is_point_on_kirby(event.position()):
            self.open_file_picker()
            event.accept()

    def leaveEvent(self, _event) -> None:
        self._toolbar_hide_timer.start(520)

    # --- Drag & Drop Image Files Directly into Kirby's Mouth ---
    def _extract_image_path(self, mime) -> Optional[str]:
        if mime is None or not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    return str(p)
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if self._extract_image_path(mime) is not None or (mime is not None and mime.hasImage()):
            event.acceptProposedAction()
            if self._state == self.STATE_IDLE:
                self._state = self.STATE_DRAG_HOVER
                self._state_t = 0.0
                self.update()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        if self._extract_image_path(mime) is not None or (mime is not None and mime.hasImage()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, _event: QDragLeaveEvent) -> None:
        if self._state == self.STATE_DRAG_HOVER:
            self._state = self.STATE_IDLE
            self._state_t = 0.0
            self.update()

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        path = self._extract_image_path(mime)
        if path is not None:
            event.acceptProposedAction()
            self.eat_file(path)
            return
        if mime is not None and mime.hasImage():
            qimg = mime.imageData()
            if qimg is not None and not qimg.isNull():
                event.acceptProposedAction()
                self.eat_pil_image(qimage_to_pil(qimg), "dropped_image")
                return
        if self._state == self.STATE_DRAG_HOVER:
            self._state = self.STATE_IDLE
        event.ignore()
