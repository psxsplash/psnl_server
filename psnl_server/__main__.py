"""Entrypoint: `python -m psnl_server` / `psnl-server`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys

from .game import load_game
from .server import SNAPSHOT_HZ, PSNLServer


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="psnl-server",
        description="PSNL central server - networked multiplayer for psxsplash on the PlayStation 1.",
        epilog=(
            "Two ports, because the two client kinds speak different wires:\n"
            "  raw       the Python bridge (real PlayStation over serial), and Redux-as-SIO1-server\n"
            "  protobuf  PCSX-Redux's SIO1 client, which cannot speak raw\n"
            "\n"
            "Redux players: set SIO1 Client + Mode=Protobuf and point it at the protobuf port.\n"
            "Do NOT choose Raw with the client - Redux throws an uncaught exception."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default=os.environ.get("PSNL_HOST", "0.0.0.0"))
    p.add_argument("--raw-port", type=int, default=_env_int("PSNL_RAW_PORT", 6699))
    p.add_argument("--protobuf-port", type=int, default=_env_int("PSNL_PROTOBUF_PORT", 6700))
    p.add_argument("--snapshot-hz", type=float, default=float(os.environ.get("PSNL_SNAPSHOT_HZ", SNAPSHOT_HZ)))
    p.add_argument("--log-level", default=os.environ.get("PSNL_LOG_LEVEL", "INFO"))
    p.add_argument(
        "--game",
        default=os.environ.get("PSNL_GAME"),
        metavar="MODULE:ATTR",
        help=(
            "Server-side game rules to attach, as 'module:attr' or 'path/to/file.py:attr'. "
            "Without one the server is a pure relay. "
            "PSXSUS: --game PSXSUS/Server/psxsus_game.py:PSXSUSGame"
        ),
    )
    return p


async def _run(args: argparse.Namespace) -> None:
    game = load_game(args.game) if args.game else None
    server = PSNLServer(
        host=args.host,
        raw_port=args.raw_port,
        protobuf_port=args.protobuf_port,
        snapshot_hz=args.snapshot_hz,
        game=game,
    )
    await server.start()
    try:
        await asyncio.Event().wait()  # run until cancelled
    finally:
        await server.stop()


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # uvloop is a meaningful speedup on Linux/Docker and simply absent on
    # Windows; its absence is never an error.
    with contextlib.suppress(ImportError):
        import uvloop

        uvloop.install()
        logging.getLogger(__name__).info("using uvloop")

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
