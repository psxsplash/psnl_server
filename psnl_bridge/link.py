"""The bridge core: serial cable <-> central server.

A real PlayStation speaks raw bytes down a serial cable - there is no framing
below PSNL and no protobuf involved. So the bridge is, at heart, a byte pump:
everything the console says goes to the server, everything the server says goes
to the console. It deliberately does NOT parse PSNL. Understanding the protocol
would mean a second implementation to keep in step with the console, and the
bridge has no reason to know what it is carrying.

Threading: pyserial is blocking, so the port is serviced on a worker thread and
hands bytes to the asyncio loop via `call_soon_threadsafe`. Qt's QSerialPort was
the alternative, but marrying Qt's event loop to asyncio is more friction than
one well-understood reader thread.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import serial

logger = logging.getLogger(__name__)

# The console's SIO1 default (Sio1::c_defaultBaud) - MUST match it.
#
# A mismatch with the console is SILENT and total: framing errors present as an
# unplugged cable, not as errors. It has already happened once -- the console held
# a second, hardcoded copy of this number, so changing it moved the bridge alone
# and the link went dead with no diagnostic. The console prints its own baud on
# the connecting screen; compare the two before assuming anything else is wrong.
#
# 57600, matching Sio1::c_defaultBaud. The limit is the PlayStation's interrupt
# latency, not the cable: at 115200 the retail BIOS dispatcher's blackout overran
# the console's 8-byte RX FIFO, which corrupted long frames only -- so the link
# looked healthy and the game looked broken. Halving the rate doubles the margin.
DEFAULT_BAUD = 57600
DEFAULT_SERVER_PORT = 6699  # the server's RAW port: a serial cable carries raw bytes

#: How many chunks may wait for the serial port before the bridge stops reading
#: the server socket. Sized in time, not memory: reads come back in units well
#: under 4 KiB, so ~32 of them is a fraction of a second of 115200 wire time. Any
#: deeper and the console would be handed positions that were stale before they
#: left the PC - latency it can never recover from, because a serial link has no
#: way to skip ahead.
SERIAL_QUEUE_DEPTH = 32

#: How long to wait before re-offering to a full queue. One 215-byte snapshot is
#: ~19ms of wire time, so this is roughly "one message drained".
SERIAL_QUEUE_WAIT = 0.02

#: Writes block until the UART drains, so a timeout is the only thing separating
#: "slow" from "wedged". Without one a dead port parks the writer thread forever
#: and the bridge reports itself healthy while carrying nothing.
SERIAL_WRITE_TIMEOUT = 5.0


@dataclass(slots=True)
class LinkStats:
    console_rx: int = 0  # bytes read from the PlayStation
    console_tx: int = 0  # bytes written to the PlayStation
    server_rx: int = 0
    server_tx: int = 0
    serial_errors: int = 0
    #: Times the pump had to wait because the serial queue was full. Non-zero
    #: means the server is outrunning the cable and backpressure is engaging -
    #: which is the system working, not failing, but it is also the number that
    #: says "this link is at its limit".
    backpressure_waits: int = 0
    connected_since: float | None = None
    last_console_byte_at: float | None = None
    last_server_byte_at: float | None = None

    def reset(self) -> None:
        self.console_rx = self.console_tx = self.server_rx = self.server_tx = 0
        self.serial_errors = 0
        self.backpressure_waits = 0
        self.connected_since = None
        self.last_console_byte_at = None
        self.last_server_byte_at = None


class SerialWriter(threading.Thread):
    """Blocking serial writes on a worker thread, fed by a BOUNDED queue.

    The bound is the point. `serial.write()` used to run directly on the asyncio
    loop, where a full OS transmit buffer parked the entire bridge - including the
    callbacks carrying console->server traffic - for as long as the UART needed to
    drain. That is backpressure by paralysis: nothing upstream learned anything,
    it just stopped being served.

    With a bounded queue the stall becomes information. When it fills, the pump
    stops reading its socket, TCP closes the receive window, the server's write
    buffer grows, and the server's own pacing check sees a peer that is behind and
    stops sending it snapshots. Backpressure runs the whole way from this queue to
    the game loop, instead of every hop buffering blindly and pretending it is fine.
    """

    daemon = True

    def __init__(
        self,
        port: serial.Serial,
        maxsize: int,
        on_error: Callable[[Exception], None],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__(name="psnl-serial-writer")
        self._port = port
        self._loop = loop
        self._on_error = on_error
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        #: Bytes accepted but not yet written. Read by the GUI: a depth that sits
        #: near the cap is the clearest possible statement that the server is
        #: producing faster than 115200 baud can carry.
        self.queued_bytes = 0
        self.written_bytes = 0
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # wake the thread out of get()
        except queue.Full:
            pass

    def submit(self, data: bytes) -> bool:
        """Queue bytes for the port. False means full - the caller must stop reading.

        Never drops: this carries framed PSNL, so discarding a slice would corrupt
        the frame around it and cost the peer two messages instead of none. Refusing
        pushes the decision up to someone who can make it correctly.
        """
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            return False
        with self._lock:
            self.queued_bytes += len(data)
        return True

    def full(self) -> bool:
        return self._queue.full()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is None:
                return
            try:
                self._port.write(data)
            except Exception as exc:
                if not self._stop.is_set():
                    self._loop.call_soon_threadsafe(self._on_error, exc)
                return
            with self._lock:
                self.queued_bytes -= len(data)
                self.written_bytes += len(data)


class SerialReader(threading.Thread):
    """Blocking serial reads on a worker thread, delivered to the asyncio loop."""

    daemon = True

    def __init__(
        self,
        port: serial.Serial,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[bytes], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        super().__init__(name="psnl-serial-reader")
        self._port = port
        self._loop = loop
        self._on_data = on_data
        self._on_error = on_error
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                # `read(1)` blocks up to the port timeout, then in_waiting lets us
                # take the rest of the burst in one go instead of byte-at-a-time.
                data = self._port.read(1)
                if not data:
                    continue
                waiting = self._port.in_waiting
                if waiting:
                    data += self._port.read(waiting)
                self._loop.call_soon_threadsafe(self._on_data, data)
            except Exception as exc:
                if not self._stop.is_set():
                    self._loop.call_soon_threadsafe(self._on_error, exc)
                return


class BridgeLink:
    """Pumps bytes between one serial port and one server connection."""

    def __init__(self) -> None:
        self.stats = LinkStats()
        self._serial: serial.Serial | None = None
        self._reader_thread: SerialReader | None = None
        self._writer_thread: SerialWriter | None = None
        self._stop_task: asyncio.Task | None = None
        self._server_reader: asyncio.StreamReader | None = None
        self._server_writer: asyncio.StreamWriter | None = None
        self._pump_task: asyncio.Task | None = None
        self._running = False
        self.on_state_change: Callable[[str], None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def serial_queue_bytes(self) -> int:
        """Bytes waiting for the cable. The single most useful number on the GUI:
        cumulative totals can hide a 48:1 send/receive imbalance entirely, whereas a
        queue that never empties says immediately that the console cannot keep up.
        """
        writer = self._writer_thread
        return writer.queued_bytes if writer is not None else 0

    def _notify(self, message: str) -> None:
        logger.info("%s", message)
        if self.on_state_change:
            self.on_state_change(message)

    async def start(self, com_port: str, baud: int, host: str, port: int) -> None:
        if self._running:
            raise RuntimeError("bridge is already running")

        loop = asyncio.get_running_loop()
        self.stats.reset()

        # A short read timeout keeps the reader thread responsive to stop() rather
        # than parking forever inside read(). The write timeout is what turns a
        # wedged port into an error instead of a silent, permanent stall.
        self._serial = serial.Serial(
            port=com_port, baudrate=baud, timeout=0.05, write_timeout=SERIAL_WRITE_TIMEOUT
        )
        self._notify(f"opened {com_port} at {baud} baud")

        try:
            self._server_reader, self._server_writer = await asyncio.open_connection(host, port)
        except Exception:
            self._serial.close()
            self._serial = None
            raise
        self._notify(f"connected to {host}:{port}")

        self._writer_thread = SerialWriter(
            self._serial, SERIAL_QUEUE_DEPTH, self._on_serial_error, loop
        )
        self._writer_thread.start()
        self._reader_thread = SerialReader(
            self._serial, loop, self._on_console_data, self._on_serial_error
        )
        self._reader_thread.start()
        self._pump_task = asyncio.create_task(self._pump_server())
        self._running = True
        self.stats.connected_since = time.monotonic()

    def _on_console_data(self, data: bytes) -> None:
        self.stats.console_rx += len(data)
        self.stats.last_console_byte_at = time.monotonic()
        if self._server_writer is not None:
            try:
                self._server_writer.write(data)
                self.stats.server_tx += len(data)
            except Exception as exc:
                self._notify(f"server write failed: {exc}")

    def _on_serial_error(self, exc: Exception) -> None:
        self.stats.serial_errors += 1
        self._notify(f"serial error: {exc}")
        # Keep a reference: a bare create_task is only weakly held by the loop and
        # can be collected mid-shutdown, which leaves the port open and the bridge
        # claiming to be running.
        self._stop_task = asyncio.create_task(self.stop())

    async def _pump_server(self) -> None:
        assert self._server_reader is not None
        try:
            while True:
                data = await self._server_reader.read(4096)
                if not data:
                    self._notify("server closed the connection")
                    break
                self.stats.server_rx += len(data)
                self.stats.last_server_byte_at = time.monotonic()
                writer = self._writer_thread
                if writer is None:
                    continue
                # Hand off to the writer thread; never write from here. A blocking
                # write on the loop stalls console->server traffic too, because
                # those arrive as loop callbacks.
                while not writer.submit(data):
                    # Queue full: the console is genuinely behind. Stop reading the
                    # socket so TCP applies real backpressure to the server, which
                    # will then pace its snapshots down. Sleeping here is the
                    # correct thing to do - this task's ONLY job is this socket.
                    self.stats.backpressure_waits += 1
                    await asyncio.sleep(SERIAL_QUEUE_WAIT)
                self.stats.console_tx += len(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._notify(f"server read failed: {exc}")
        finally:
            if self._running:
                await self.stop()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._reader_thread is not None:
            self._reader_thread.stop()
            self._reader_thread = None
        if self._writer_thread is not None:
            self._writer_thread.stop()
            self._writer_thread = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        if self._server_writer is not None:
            try:
                self._server_writer.close()
            except Exception:
                pass
            self._server_writer = None
        self._server_reader = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.stats.connected_since = None
        self._notify("disconnected")


def list_serial_ports() -> list[tuple[str, str]]:
    """-> [(device, human description)]"""
    from serial.tools import list_ports

    return [(p.device, p.description or p.device) for p in list_ports.comports()]
