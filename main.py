"""Entry point for Kirby Desktop Background Remover (星之卡比去背精靈)."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from kirby_widget import KirbyMascotWindow

SINGLE_INSTANCE_KEY = "kirby_bg_remover_single_instance_v1"


def _set_windows_app_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Peiyu.KirbyBgRemover.1.0"
        )
    except Exception:
        pass


def main() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("星之卡比去背精靈")
    app.setQuitOnLastWindowClosed(False)

    # Single-instance check: if already running, wake existing Kirby (and pass file if any)
    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(250):
        payload = f"EAT:{sys.argv[1]}" if len(sys.argv) > 1 else "SHOW"
        probe.write(payload.encode("utf-8"))
        probe.flush()
        probe.waitForBytesWritten(250)
        probe.disconnectFromServer()
        return 0

    # Clear stale socket if previous run crashed
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer(app)
    server.listen(SINGLE_INSTANCE_KEY)

    assets_dir = Path(__file__).resolve().parent / "assets"
    icon_path = assets_dir / "kirby.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    kirby = KirbyMascotWindow(assets_dir)
    kirby.show()

    def _on_new_instance() -> None:
        sock = server.nextPendingConnection()
        msg = "SHOW"
        if sock:
            sock.waitForReadyRead(150)
            msg = bytes(sock.readAll()).decode("utf-8", errors="ignore").strip()
            sock.disconnectFromServer()
            sock.deleteLater()
        kirby.bring_to_front()
        if msg.startswith("EAT:"):
            p = Path(msg[4:])
            if p.is_file():
                kirby.eat_file(str(p))

    server.newConnection.connect(_on_new_instance)

    # Support dropping a file directly onto the desktop shortcut icon!
    if len(sys.argv) > 1:
        arg_path = Path(sys.argv[1])
        if arg_path.is_file():
            from PySide6.QtCore import QTimer

            QTimer.singleShot(350, lambda: kirby.eat_file(str(arg_path)))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
