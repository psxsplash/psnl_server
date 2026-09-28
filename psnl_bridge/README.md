# psnl-bridge

Connects a **real PlayStation** to the PSNL central server over a serial cable.

```
   PlayStation --SIO1 serial cable--> [bridge] --TCP :6699--> psnl-server
```

> **On PCSX-Redux you do not need this.** Point the emulator's built-in SIO1
> **client** (Mode: **Protobuf**) at the server's protobuf port (6700). This
> bridge exists for real hardware, where the cable carries raw bytes and nothing
> else.

## Run it

```sh
pip install -e ".[gui]"
python -m psnl_bridge                 # GUI: pick a port, a server, press Connect

python -m psnl_bridge --list-ports    # find your USB-serial adapter
python -m psnl_bridge --headless --port COM3 --host 192.168.1.10
```

The GUI is an optional extra, so `--headless` works on a machine with no display
and in CI without dragging in Qt.

## What it is

A byte pump, and deliberately nothing more: everything the console says goes to
the server, everything the server says goes to the console. It does **not** parse
PSNL. Understanding the protocol would make it a second implementation to keep in
step with the console and the server, for no benefit - it has no reason to know
what it is carrying.

`pyserial` is blocking, so the port is serviced on a reader thread that hands
bytes to the asyncio loop via `call_soon_threadsafe`. Qt's `QSerialPort` was the
alternative, but marrying Qt's event loop to asyncio is more friction than one
well-understood thread.

## Reading the GUI

The link is invisible when healthy, so the window answers exactly one question:
*is it working?* - and, when it isn't, *which end is silent?*

| Status | Meaning |
|---|---|
| **Connected - traffic flowing both ways** | Working. |
| **Server OK - no data from the PlayStation** | The console isn't sending. Is the game running? Is the cable on SIO1, not the controller port? |
| **PlayStation OK - server is silent** | The console is fine; check the server and the host/port. |
| **Waiting for traffic** | Connected, nothing moving yet. |

Byte counters that have stopped climbing are shown as *not flowing* rather than
as a healthy-looking total - a dead link with a big number is the failure mode
worth catching.

## Tests

```sh
pytest tests -q
```

A socket pair stands in for the cable, so the real `BridgeLink`, the real server
and the real framing all take part end to end. On a machine with **com0com**
(Windows) or **socat** (Linux) you can point `--port` at a genuine virtual serial
pair and the same path runs unchanged.
