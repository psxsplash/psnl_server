"""The bridge's desktop GUI.

Deliberately one window with no tabs, no wizard, and no modes. The user has a
PlayStation on a serial cable and wants to play: pick a port, pick a server,
press Connect. Everything else on screen exists to answer "is it working?" -
which, for a link that is invisible when healthy, is the only question that
matters when it isn't.
"""

from __future__ import annotations

import asyncio
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .link import DEFAULT_BAUD, DEFAULT_SERVER_PORT, BridgeLink, list_serial_ports

_MONO = "Consolas, 'DejaVu Sans Mono', monospace"

# Bytes are only "flowing" if we saw some recently; a total that stopped climbing
# two seconds ago is a dead link showing a healthy-looking number.
_FLOW_WINDOW = 2.0


class StatusDot(QLabel):
    """A coloured dot. The one thing a user reads from across the room."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(14, 14)
        self.set_state("idle")

    def set_state(self, state: str) -> None:
        colour = {"idle": "#6b7280", "live": "#22c55e", "error": "#ef4444", "wait": "#f59e0b"}[state]
        self.setStyleSheet(f"background:{colour}; border-radius:7px;")


class BridgeWindow(QWidget):
    def __init__(self, link: BridgeLink) -> None:
        super().__init__()
        self._link = link
        self._link.on_state_change = self._log
        self.setWindowTitle("PSXSUS Bridge - PlayStation <-> Server")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        # --- connection ---
        conn = QGroupBox("Connection")
        form = QFormLayout(conn)

        port_row = QHBoxLayout()
        self._port = QComboBox()
        self._port.setMinimumWidth(280)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_ports)
        port_row.addWidget(self._port, 1)
        port_row.addWidget(refresh)
        form.addRow("Serial port", port_row)

        self._baud = QComboBox()
        for b in (9600, 19200, 38400, 57600, 115200):
            self._baud.addItem(str(b), b)
        self._baud.setCurrentText(str(DEFAULT_BAUD))
        form.addRow("Baud", self._baud)

        self._host = QLineEdit("127.0.0.1")
        form.addRow("Server host", self._host)

        self._server_port = QSpinBox()
        self._server_port.setRange(1, 65535)
        self._server_port.setValue(DEFAULT_SERVER_PORT)
        form.addRow("Server port", self._server_port)

        root.addWidget(conn)

        # --- action ---
        action = QHBoxLayout()
        self._dot = StatusDot()
        self._status = QLabel("Not connected")
        self._connect = QPushButton("Connect")
        self._connect.setDefault(True)
        self._connect.clicked.connect(self._toggle)
        action.addWidget(self._dot)
        action.addWidget(self._status, 1)
        action.addWidget(self._connect)
        root.addLayout(action)

        # --- traffic ---
        traffic = QGroupBox("Link health")
        grid = QGridLayout(traffic)
        self._counters: dict[str, QLabel] = {}
        rows = [
            ("From PlayStation", "console_rx"),
            ("To PlayStation", "console_tx"),
            ("From server", "server_rx"),
            ("To server", "server_tx"),
        ]
        for i, (label, key) in enumerate(rows):
            grid.addWidget(QLabel(label), i, 0)
            value = QLabel("0 B")
            value.setFont(QFont(_MONO.split(",")[0], 10))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(value, i, 1)
            self._counters[key] = value
        grid.addWidget(QLabel("Serial backlog"), len(rows), 0)
        self._queue_label = QLabel("0 B waiting for the cable")
        self._queue_label.setFont(QFont(_MONO.split(",")[0], 10))
        self._queue_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(self._queue_label, len(rows), 1)
        root.addWidget(traffic)

        # Previous sample, for turning cumulative counters into rates.
        self._last_sample: dict[str, int] = {}
        self._last_sample_at: float | None = None

        # --- log ---
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        self._log_view.setFrameShape(QFrame.NoFrame)
        self._log_view.setStyleSheet(f"font-family:{_MONO}; font-size:11px;")
        self._log_view.setFixedHeight(120)
        root.addWidget(self._log_view)

        hint = QLabel(
            "Playing on PCSX-Redux instead? You don't need this bridge - set "
            "<b>SIO1 Client</b> + <b>Mode: Protobuf</b> and point it at the server's "
            "protobuf port (6700).<br>Redux's client cannot use <b>Raw</b> mode."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6b7280; font-size:11px;")
        root.addWidget(hint)

        self._refresh_ports()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_stats)
        self._timer.start(250)

    def _log(self, message: str) -> None:
        self._log_view.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def _refresh_ports(self) -> None:
        current = self._port.currentData()
        self._port.clear()
        ports = list_serial_ports()
        if not ports:
            self._port.addItem("No serial ports found", None)
            self._port.setEnabled(False)
            return
        self._port.setEnabled(True)
        for device, description in ports:
            self._port.addItem(f"{device} - {description}", device)
        if current:
            index = self._port.findData(current)
            if index >= 0:
                self._port.setCurrentIndex(index)

    def _toggle(self) -> None:
        if self._link.running:
            asyncio.ensure_future(self._do_stop())
        else:
            asyncio.ensure_future(self._do_start())

    async def _do_start(self) -> None:
        device = self._port.currentData()
        if not device:
            QMessageBox.warning(self, "No serial port", "Connect your PlayStation's serial cable first.")
            return
        self._connect.setEnabled(False)
        self._dot.set_state("wait")
        self._status.setText("Connecting...")
        try:
            await self._link.start(
                device, self._baud.currentData(), self._host.text().strip(), self._server_port.value()
            )
        except Exception as exc:
            self._dot.set_state("error")
            self._status.setText("Connection failed")
            self._log(f"error: {exc}")
            QMessageBox.critical(self, "Could not connect", str(exc))
        finally:
            self._connect.setEnabled(True)
            self._connect.setText("Disconnect" if self._link.running else "Connect")

    async def _do_stop(self) -> None:
        await self._link.stop()
        self._connect.setText("Connect")
        self._dot.set_state("idle")
        self._status.setText("Not connected")

    def _refresh_stats(self) -> None:
        stats = self._link.stats
        now = time.monotonic()

        # Show a RATE beside each total. Totals alone are close to useless for
        # diagnosis: a link sending 6.2 KB/s at a console that can only answer at
        # 0.1 KB/s looked, cumulatively, like "342 KB out, 7 KB in" - a 48:1
        # imbalance completely. As rates it is obvious on sight.
        elapsed = now - self._last_sample_at if self._last_sample_at else 0.0
        for key, label in self._counters.items():
            total = getattr(stats, key)
            text = _human_bytes(total)
            if elapsed >= 0.2:
                rate = (total - self._last_sample.get(key, total)) / elapsed
                if rate >= 1.0:
                    text += f"   ({_human_bytes(int(rate))}/s)"
            label.setText(text)
        if elapsed >= 0.2:
            self._last_sample = {key: getattr(stats, key) for key in self._counters}
            self._last_sample_at = now
        elif self._last_sample_at is None:
            self._last_sample = {key: getattr(stats, key) for key in self._counters}
            self._last_sample_at = now

        # Bytes stuck waiting for the cable. This is what a saturated serial link
        # looks like from the PC side, and nothing used to report it at all.
        queued = self._link.serial_queue_bytes
        self._queue_label.setText(
            f"{_human_bytes(queued)} waiting for the cable"
            + (f"   ({stats.backpressure_waits} stalls)" if stats.backpressure_waits else "")
        )

        if not self._link.running:
            self._connect.setText("Connect")
            return

        console_live = stats.last_console_byte_at and now - stats.last_console_byte_at < _FLOW_WINDOW
        server_live = stats.last_server_byte_at and now - stats.last_server_byte_at < _FLOW_WINDOW

        # Distinguish the failure modes rather than showing a generic "connected":
        # a silent console and a silent server need completely different fixes.
        if console_live and server_live:
            self._dot.set_state("live")
            self._status.setText("Connected - traffic flowing both ways")
        elif server_live and not console_live:
            self._dot.set_state("wait")
            self._status.setText("Server OK - no data from the PlayStation (is the game running?)")
        elif console_live and not server_live:
            self._dot.set_state("wait")
            self._status.setText("PlayStation OK - server is silent")
        else:
            self._dot.set_state("wait")
            self._status.setText("Connected - waiting for traffic")


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def run_gui() -> int:
    import qasync

    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    link = BridgeLink()
    window = BridgeWindow(link)
    window.show()

    with loop:
        loop.run_forever()
    return 0
