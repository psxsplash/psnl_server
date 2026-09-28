"""A very small HTTP + Server-Sent-Events server, on the stdlib alone.

Two constraints shaped this, and both are worth stating because they rule out
the obvious answers:

* **`psnl` declares no runtime dependencies** (see pyproject.toml, and the note
  there about the image being buildable from a clone of itself). A spectator
  page is not worth making aiohttp or starlette a hard requirement of running a
  PlayStation relay, so this is `asyncio.start_server` and about two hundred
  lines.
* **The live feed only ever flows one way.** The spectator watches; it never
  sends anything back. That makes Server-Sent Events strictly better than a
  WebSocket here: no upgrade handshake, no frame masking, no ping/pong, no
  close-code state machine - it is `text/event-stream` on an ordinary GET, and
  the browser reconnects by itself when it drops. The whole implementation is
  `_stream()` below.

This is deliberately NOT a general-purpose web server. GET and HEAD only, no
request bodies, no chunked encoding, no keep-alive pipelining beyond one request
per connection. Anything more is surface area on a service whose entire job is
to hand out three JSON documents and some PNGs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

logger = logging.getLogger(__name__)

#: A request line plus headers. Anything larger is not a browser asking for a
#: page, so it is refused rather than buffered.
MAX_REQUEST_BYTES = 16 * 1024

#: How long a connection may take to send its request line and headers.
REQUEST_TIMEOUT = 15.0

STATUS_TEXT = {
    200: "OK", 204: "No Content", 304: "Not Modified", 400: "Bad Request",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    408: "Request Timeout", 413: "Payload Too Large", 500: "Internal Server Error",
    503: "Service Unavailable",
}


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    #: The peer's address as the SOCKET sees it. Behind a reverse proxy this is
    #: the proxy, which is why `client_ip` exists.
    peer_ip: str
    #: Who the request is really from. Identical to `peer_ip` unless the peer is
    #: a configured trusted proxy - see `HttpServer.trusted_proxies`. This is
    #: what the fair-play check compares against a console's address.
    client_ip: str = ""
    match: re.Match | None = None

    def param(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default


def resolve_client_ip(peer_ip: str, headers: dict[str, str],
                      trusted: frozenset[str]) -> str:
    """The real client address, taking forwarding headers ONLY from a proxy we
    were told to trust.

    Never trusted by default, and the reason is the fair-play warning: it is
    driven entirely by comparing this address against the ones consoles connect
    from, so a client free to set `X-Forwarded-For` is a client free to decide
    whether it gets warned. Requiring the socket peer to be a configured proxy
    first means the header can only come from our own nginx, which overwrites
    whatever the browser sent.

    `X-Real-IP` is preferred because nginx assigns it `$remote_addr` outright.
    `X-Forwarded-For` is a chain the client can prepend to, so only its LAST
    entry - the hop our proxy actually observed - means anything.
    """
    if not trusted or peer_ip not in trusted:
        return peer_ip
    real = headers.get("x-real-ip", "").strip()
    if real:
        return real
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return peer_ip


@dataclass(slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data, status: int = 200) -> "Response":
        return cls(
            status=status,
            body=json.dumps(data, separators=(",", ":")).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            # Live state. A cached room list is a room list showing players who
            # left ten minutes ago.
            headers={"Cache-Control": "no-store"},
        )

    @classmethod
    def error(cls, status: int) -> "Response":
        return cls(status=status, body=f"{status} {STATUS_TEXT.get(status, '')}\n".encode())


class Stream:
    """One live Server-Sent-Events connection.

    A handler is handed one of these and pushes to it until `closed` goes True,
    which happens when the browser navigates away. Detecting that is the only
    subtle part: a TCP write to a peer that has gone can succeed for a long time
    (it lands in the kernel buffer), so a stream that only ever wrote would keep
    a dead spectator's feed alive indefinitely. `_watch_eof` reads the socket -
    which an SSE client never writes to - purely so that EOF closes us promptly.
    """

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self.closed = False

    async def send(self, data, event: str | None = None) -> bool:
        """One SSE event. Returns False once the peer has gone."""
        if self.closed:
            return False
        payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        chunk = ""
        if event:
            chunk += f"event: {event}\n"
        # A payload containing a newline would end the event early, so every line
        # gets its own `data:` prefix. JSON never contains a raw newline, but the
        # rule is cheap and the failure it prevents is silent truncation.
        for line in payload.split("\n"):
            chunk += f"data: {line}\n"
        chunk += "\n"
        try:
            self._writer.write(chunk.encode("utf-8"))
            await self._writer.drain()
        except (ConnectionError, RuntimeError):
            self.closed = True
            return False
        return True


#: A route whose handler streams instead of returning a Response.
STREAM = "stream"


class HttpServer:
    def __init__(self, host: str, port: int, *, static_dir: Path | None = None,
                 trusted_proxies: frozenset[str] | None = None) -> None:
        self.host = host
        self.port = port
        self.static_dir = static_dir.resolve() if static_dir else None
        #: Peers whose forwarding headers are believed. Empty means believe
        #: nobody, which is the only safe default - see `resolve_client_ip`.
        self.trusted_proxies = trusted_proxies or frozenset()
        self._routes: list[tuple[str, re.Pattern, object, bool]] = []
        self._server: asyncio.AbstractServer | None = None
        #: Every live SSE stream, so shutdown can end them instead of waiting for
        #: connections that by design never finish.
        self._streams: set[asyncio.Task] = set()

    # --- registration ---------------------------------------------------

    def route(self, pattern: str, handler, *, stream: bool = False, method: str = "GET") -> None:
        self._routes.append((method, re.compile(f"^{pattern}$"), handler, stream))

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        # Ask the socket, because port 0 is a legitimate and useful request (the
        # tests use it) and reporting "listening on 0" would be a lie.
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("psxsus spectator: http://%s:%d/", self.host, self.port)

    async def stop(self) -> None:
        for task in list(self._streams):
            task.cancel()
        self._streams.clear()
        if self._server is not None:
            self._server.close()
            # A wedged connection must not hang shutdown for good - the same
            # posture PSNLServer.stop takes with its own listeners.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            self._server = None

    # --- serving --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader, writer)
            if request is None:
                return

            handler, streaming = self._match(request)
            if handler is None:
                await self._respond(writer, request, self._static(request))
                return

            if streaming:
                await self._stream(reader, writer, request, handler)
                return

            try:
                response = handler(request)
                if asyncio.iscoroutine(response):
                    response = await response
            except Exception:
                logger.exception("spectator: handler failed for %s", request.path)
                response = Response.error(500)
            await self._respond(writer, request, response)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("spectator: connection error")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _read_request(self, reader, writer) -> Request | None:
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            await self._respond(writer, None, Response.error(408))
            return None
        except asyncio.LimitOverrunError:
            await self._respond(writer, None, Response.error(413))
            return None
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        if len(head) > MAX_REQUEST_BYTES:
            await self._respond(writer, None, Response.error(413))
            return None

        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            await self._respond(writer, None, Response.error(400))
            return None
        method, target, _version = parts
        if method not in ("GET", "HEAD"):
            await self._respond(writer, None, Response.error(405))
            return None

        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        split = urlsplit(target)
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        return Request(
            method=method,
            path=unquote(split.path),
            query=parse_qs(split.query),
            headers=headers,
            peer_ip=peer_ip,
            client_ip=resolve_client_ip(peer_ip, headers, self.trusted_proxies),
        )

    def _match(self, request: Request):
        for method, pattern, handler, streaming in self._routes:
            if method != request.method and not (method == "GET" and request.method == "HEAD"):
                continue
            m = pattern.match(request.path)
            if m:
                request.match = m
                return handler, streaming
        return None, False

    async def _respond(self, writer, request: Request | None, response: Response) -> None:
        body = b"" if (request is not None and request.method == "HEAD") else response.body
        head = [f"HTTP/1.1 {response.status} {STATUS_TEXT.get(response.status, '')}"]
        head.append(f"Content-Type: {response.content_type}")
        head.append(f"Content-Length: {len(response.body)}")
        # One request per connection. Keep-alive would need this server to be
        # careful about framing every response exactly right, for no gain: the
        # page makes a handful of requests and then holds one long stream.
        head.append("Connection: close")
        for name, value in response.headers.items():
            head.append(f"{name}: {value}")
        try:
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    async def _stream(self, reader, writer, request: Request, handler) -> None:
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            # Nginx buffers proxied responses by default, which for an event
            # stream means the page shows nothing until the buffer fills - i.e.
            # a spectator view that appears frozen behind a reverse proxy and
            # works perfectly when tested directly.
            "X-Accel-Buffering: no\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            writer.write(head.encode("latin-1"))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            return

        stream = Stream(writer)
        watcher = asyncio.create_task(self._watch_eof(reader, stream))
        task = asyncio.current_task()
        if task is not None:
            self._streams.add(task)
        try:
            await handler(request, stream)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("spectator: stream handler failed for %s", request.path)
        finally:
            stream.closed = True
            watcher.cancel()
            if task is not None:
                self._streams.discard(task)

    @staticmethod
    async def _watch_eof(reader: asyncio.StreamReader, stream: Stream) -> None:
        """Close the stream as soon as the browser goes away.

        An SSE client sends nothing after its request, so anything we read here
        is either a pipelined request we do not serve or EOF. Either way the
        answer is to stop."""
        try:
            await reader.read(1)
        except Exception:
            pass
        stream.closed = True

    # --- static files ---------------------------------------------------

    def _static(self, request: Request) -> Response:
        if self.static_dir is None:
            return Response.error(404)
        rel = request.path.lstrip("/") or "index.html"
        candidate = (self.static_dir / rel).resolve()
        # Containment check, not a string test: `..` is only one way to escape,
        # and a symlink is another.
        print(f"DEBUG: static_dir={self.static_dir} | candidate={candidate} | exists={candidate.is_file()}")

        if not candidate.is_relative_to(self.static_dir) or not candidate.is_file():
            return Response.error(404)

        data = candidate.read_bytes()
        ctype, _ = mimetypes.guess_type(candidate.name)
        headers = {}
        if candidate.suffix in (".png", ".json"):
            # The maps and sheets are regenerated only by a pipeline run, and a
            # spectator loading a 10KB map on every pan is a spectator on a
            # slower page than they need to be.
            headers["Cache-Control"] = "public, max-age=300"
        else:
            headers["Cache-Control"] = "no-cache"
        return Response(
            body=data,
            content_type=ctype or "application/octet-stream",
            headers=headers,
        )


def now_ms() -> int:
    return int(time.time() * 1000)
