"""Entrypoint: `python -m psnl_bridge` / `psnl-bridge`.

`--headless` runs the same BridgeLink without Qt, so CI (and anyone on a machine
with no display) exercises the byte pump rather than a GUI-shaped stub.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from .link import DEFAULT_BAUD, DEFAULT_SERVER_PORT, BridgeLink, list_serial_ports


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="psnl-bridge",
        description="Bridges a real PlayStation's serial port to the PSNL central server.",
        epilog=(
            "Playing on PCSX-Redux? You do not need this bridge: set SIO1 Client +\n"
            "Mode=Protobuf and point it at the server's protobuf port (6700)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    p.add_argument("--headless", action="store_true", help="run without the GUI")
    p.add_argument("--port", help="serial port (e.g. COM3, /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--host", default="127.0.0.1", help="central server host")
    p.add_argument(
        "--server-port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help="central server RAW port (a serial cable carries raw bytes)",
    )
    p.add_argument("--log-level", default="INFO")
    return p


async def _run_headless(args: argparse.Namespace) -> int:
    if not args.port:
        print("--headless requires --port (see --list-ports)", file=sys.stderr)
        return 2
    link = BridgeLink()
    link.on_state_change = lambda m: logging.getLogger("bridge").info("%s", m)
    await link.start(args.port, args.baud, args.host, args.server_port)
    try:
        while link.running:
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await link.stop()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found.")
        for device, description in ports:
            print(f"{device}\t{description}")
        return 0

    if args.headless:
        try:
            return asyncio.run(_run_headless(args))
        except KeyboardInterrupt:
            return 0

    try:
        from .gui import run_gui
    except ImportError as exc:
        print(f"GUI unavailable ({exc}). Install the GUI extra, or use --headless.", file=sys.stderr)
        return 2

    with contextlib.suppress(KeyboardInterrupt):
        return run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
