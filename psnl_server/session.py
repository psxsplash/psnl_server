"""Per-connection session: handshake, slot assignment, and packet dispatch."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Callable

from psnl import (
    FLAG_REBIND,
    HOST_SLOT,
    MAGIC,
    NO_SLOT,
    PROTO_VERSION,
    Frame,
    FrameParser,
    HelloAckPayload,
    HelloPayload,
    PacketType,
    PingPayload,
    encode_frame,
)

from .rooms import (
    RECORD_SIZE,
    Avatar,
    Member,
    Room,
    RoomManager,
    SceneMismatchError,
    RoomFullError,
)
from .transport.framing import FramingAdapter

logger = logging.getLogger(__name__)

#: How long to wait for a console's ack before the FIRST resend. Comfortably
#: longer than a round trip at 115200 (a 163-byte roster is ~14ms of wire time
#: each way) so a healthy link never retransmits.
RELIABLE_RETRY_SECONDS = 0.30

#: Each successive resend waits this much longer than the last, capped below.
#:
#: A flat 0.30s retry was wrong in the case that matters. Retransmitting on a
#: schedule shorter than the actual round trip does not repair a congested link,
#: it adds to the congestion - and the link is congested precisely when the
#: message is large, which is when it is also important. Backing off lets the
#: queue drain instead of feeding it.
RELIABLE_RETRY_BACKOFF = 1.6
RELIABLE_RETRY_MAX_SECONDS = 2.0

#: How much longer than the REPORTED round trip to wait before resending.
#:
#: A reported RTT is a smoothed average, and it is the tail that expires a timer.
#: Retransmitting at exactly the mean means resending roughly half of all
#: messages that were going to arrive anyway.
RELIABLE_RTT_MARGIN = 2.0

#: Sync byte + 8-byte header + 2-byte CRC, per netprotocol.hh. Used to price a
#: message in wire time.
FRAME_OVERHEAD_BYTES = 11

#: What the console's return path costs, when it has never told us.
#:
#: NOT the cable's rate: the console's transmit path is capped at 48 bytes per
#: rendered frame (Sio1::c_txPollBytes), so its outbound ceiling is ~1.4 KB/s
#: whatever the cable can do - and an acknowledgement queues behind whatever is
#: already in the 1024-byte ring. Half a second is the conservative reading of
#: that, and conservative is the correct direction: a resend that is late costs
#: one message's latency, a resend that is early costs the link.
CONSOLE_ACK_LATENCY_SECONDS = 0.5

#: Give up after this many resends.
#:
#: This was 6 at a flat 0.30s, so the server abandoned a message 2.1 seconds in.
#: On a serial link with any queueing at all that is not a recovery policy, it is
#: a guarantee of loss: the console had not even received the first copy yet. With
#: backoff, this budget spans ~10s, which outlasts a real stall. Anything the game
#: cannot afford to lose must ALSO have a game-level retry - see
#: `on_reliable_dropped`, which is how the room roster survives exhaustion.
RELIABLE_MAX_ATTEMPTS = 8

#: Cap on unacknowledged messages held per session, so a silent console cannot
#: grow the server's memory without bound.
RELIABLE_WINDOW = 32

#: Outbound bytes queued for a peer beyond which we stop sending it snapshots.
#:
#: Sized in wire time, not bytes: at 115200 baud this is ~350ms of serial. Past
#: that the console is receiving positions that were already stale when they were
#: queued, so continuing to send only pushes the useful traffic further back. A
#: TCP peer on a LAN never accumulates this much.
SNAPSHOT_BACKLOG_LIMIT = 4096

#: Where every session's pacing STARTS, before the link has said anything.
#:
#: This replaces SERIAL_SNAPSHOT_DIVISOR, which was 2 for a "raw" adapter and 1
#: for a protobuf one - i.e. the send rate was chosen by asking what KIND of
#: transport this was and assuming the rest. That assumption was written when a
#: snapshot was 215 bytes; it is 127 now, and it was never right for a fast serial
#: link or a slow emulator host anyway.
#:
#: One path for both adapters, and it adapts. Starting conservative and opening up
#: is the safe direction: a link that turns out to be fast reaches full rate within
#: a couple of seconds, whereas one that starts at full rate can bury a fragile
#: console before the first measurement ever arrives.
PACING_INITIAL_DIVISOR = 2
PACING_MIN_DIVISOR = 1
PACING_MAX_DIVISOR = 8

#: Queueing delay, in milliseconds, that the pacer tries to stay under.
#:
#: DELAY, not throughput or loss, is the signal - this is the Vegas/LEDBAT idea.
#: Sending faster than a link drains does not fail, it QUEUES, and the queue shows
#: up as round-trip time above the link's own propagation delay long before
#: anything is dropped. That makes rising RTT an early warning where a loss
#: counter is a post-mortem.
#:
#: It is also the only congestion signal that survives this particular topology.
#: The server's own view stops at `get_write_buffer_size()`, which cannot see past
#: the kernel socket buffer, let alone through a bridge process and down a serial
#: cable to an 8-byte hardware FIFO. The console can see all of it, and RTT is how
#: it says so.
PACING_TARGET_QUEUE_MILLIS = 100

#: Hysteresis: open up only when the queue is comfortably clear, so the divisor
#: does not oscillate every Ping. Backing off is deliberately faster than opening
#: up - the cost of being briefly too slow is coarser remote motion, and the cost
#: of being too fast is a console dropping the room roster.
PACING_RELIEF_QUEUE_MILLIS = 40

#: How much a new minimum defers to history when tracking the link's baseline
#: delay. The baseline should follow a genuinely faster path but must not be
#: dragged down by one lucky sample, or every later reading looks congested.
PACING_BASE_RTT_DECAY = 16

#: A much lower bar, applied only while a reliable message is in flight: if the
#: wire is contended at all, the reliable message gets it. This is what stops the
#: room roster from queueing behind position updates the console will discard.
RELIABLE_PRIORITY_BACKLOG = 1024

#: Bytes per second the wire to a SERIAL peer can actually carry: 57600 8N1 is
#: ten bits per byte, so 5760.
#:
#: MUST MATCH Sio1::c_defaultBaud. It is derived here rather than duplicated as a
#: byte figure so a baud change moves both.
SERIAL_BAUD = 57600
SERIAL_LINE_BYTES_PER_SECOND = SERIAL_BAUD // 10

#: Fraction of the line rate the server will actually offer.
#:
#: NOT 100%, and the difference is the whole point of pacing rather than merely
#: capping. A queue offered exactly as much as it can drain never gets shorter: any
#: burst that puts bytes in it stays in it, and the delay it adds is permanent.
#: Leaving headroom is what lets the queue empty between bursts, which is what
#: keeps an acknowledgement's round trip bounded - and the round trip is what the
#: retransmit timer is racing.
#:
#: 85% is the conventional operating point for exactly this reason. On this link it
#: gives up ~860 B/s of theoretical throughput, which buys back far more by keeping
#: retransmissions from firing at all.
SERIAL_UTILISATION_PERCENT = 85
SERIAL_BYTES_PER_SECOND = SERIAL_LINE_BYTES_PER_SECOND * SERIAL_UTILISATION_PERCENT // 100

#: How much unsent budget may accumulate, as a fraction of a second. A little
#: burst absorbs the natural lumpiness of a 30Hz tick; a lot of burst is just the
#: bug this exists to prevent, arriving later.
BUDGET_BURST_SECONDS = 0.25

#: What a snapshot is assumed to cost when deciding whether to build one.
#:
#: Asked BEFORE the frame exists, so it has to be an estimate. Ten players in the
#: compact format is 4 + 12 + 10*10 = 116 bytes plus 11 of framing; this rounds up,
#: because over-estimating skips a droppable packet and under-estimating puts
#: bytes on a wire that cannot carry them, which is the failure being fixed.
SNAPSHOT_BUDGET_ESTIMATE = 140


class ByteBudget:
    """A token bucket that refuses to offer a link more bytes than it can carry.

    WHY THIS EXISTS, when the file above already has two congestion mechanisms.

    Both of the others are FEEDBACK: `outbound_backlog()` watches the local socket
    buffer, and `_adapt_pacing()` watches the round trip the console reports. Each
    needs the far end to be reachable in order to tell us it is not, and on this
    topology neither survives the case that matters.

      * `outbound_backlog()` stops at the kernel socket. Past it lie a bridge
        process holding up to SERIAL_QUEUE_DEPTH chunks and a serial cable. The
        bridge accepts tens of kilobytes before TCP ever closes its window, so the
        server reads a healthy buffer while the console is twenty seconds behind.

      * `_adapt_pacing()` is only ever called from `_on_ping`, and the console
        cannot SEND a Ping when its transmit ring is full -- which netlink.cpp
        records as permanent during play: "in game the outbound ring sits
        permanently full, so every Ping was refused, no Ping ever went out, no
        Pong ever came back". The brake is disabled exactly when it is needed.

    MEASURED, 2026-07-27, real console via the bridge: 6.7 KB from the console
    against 66.2 KB to it. The console's transmit path is capped at 48 bytes per
    frame, so 6.7 KB is it running flat out; 66.2 KB is about 11.5 seconds of wire
    time. The link was offered roughly twice what it could carry, the queue grew
    without bound, acks arrived far too late to stop retransmissions, and the
    round collapsed.

    So this is FEED-FORWARD, and that is the whole point: the link rate is a fact
    the server already knows and does not have to be told. Nothing has to get back
    from the console for it to work, which is the one property the other two lack.

    Reliable traffic is never refused -- it is the traffic that must arrive -- but
    it does CHARGE the bucket, so position updates yield to a roster automatically
    instead of needing a threshold to say so.
    """

    def __init__(self, bytes_per_second: int, now: float) -> None:
        #: 0 means unmetered: a TCP peer on a LAN has no wire worth modelling, and
        #: guessing a limit for it would only slow down the emulator.
        self.rate = bytes_per_second
        self.capacity = int(bytes_per_second * BUDGET_BURST_SECONDS)
        self.tokens = float(self.capacity)
        self.updated_at = now
        #: Bytes refused. A steadily climbing count on a serial peer is the pacer
        #: working; zero on a peer that is visibly behind means it is not engaged.
        self.refused = 0

    @property
    def metered(self) -> bool:
        return self.rate > 0

    def refill(self, now: float) -> None:
        if not self.metered:
            return
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.updated_at = now
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.rate)

    def can_afford(self, size: int) -> bool:
        return not self.metered or self.tokens >= size

    def charge(self, size: int) -> None:
        """Spend, and let the bucket go NEGATIVE.

        Reliable traffic is sent whether or not it can be afforded, so the debt has
        to be recorded somewhere or it is simply not paced. Going negative means the
        next droppable thing waits until the wire has caught up -- which is exactly
        the behaviour wanted, and it is why `can_afford` is a separate question from
        `charge` rather than one call that does both.
        """
        if self.metered:
            self.tokens -= size


class Session:
    """One connected console (real hardware via the bridge, or Redux).

    The server is always slot 0 and always the authority, so it never sends
    Hello and never participates in the peer-to-peer host election. It only ever
    *answers* with HelloAck - which is why the console's election branch (with
    its hardcoded `ack.yourSlot = 1`, networkmanager.cpp:140) is unreachable in
    the star topology, and why supporting 10 players needed no console-side
    handshake changes at all.
    """

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        adapter: FramingAdapter,
        rooms: RoomManager,
        *,
        auto_join: bool = True,
        on_connect: Callable[["Session"], None] | None = None,
    ) -> None:
        self._writer = writer
        self._adapter = adapter
        self._rooms = rooms
        self._parser = FrameParser()
        self._auto_join = auto_join
        self._on_connect = on_connect

        self.slot: int = NO_SLOT
        self.room: Room | None = None
        self.member: Member | None = None
        self.scene_hash: int = 0
        self.connected = False
        self.peer = writer.get_extra_info("peername")

        #: When this peer last sent us anything. See `is_stale` - a reset console
        #: leaves its bridge's TCP connection open, so silence is the only
        #: evidence the server ever gets that it is gone.
        self.last_rx_at = time.monotonic()

        self._tx_seq = 1
        self._reliable_rx_seq: int | None = None

        #: Reliable AppData waiting to go out, and the one currently in flight
        #: ([seq, payload, next_retry_at, attempts] or None).
        #:
        #: This direction used to be fire-and-forget while console -> server was
        #: fully reliable, an asymmetry with a nasty failure mode: the biggest
        #: message on the wire is the room roster (3 + 16*N bytes, ~163 at ten
        #: players, against 4 for a meeting announcement), corruption probability
        #: scales with frame length, and the roster is a one-shot. Losing it left
        #: a console that could not name anyone in the vote and an impostor who
        #: could not kill, with no way to recover for the whole round.
        self._outbox: list[bytes] = []
        self._inflight: list | None = None

        #: What the CONSOLE measured and told us, via Ping. Zero until the first
        #: one arrives (and forever, for a peer that never sends them).
        #:
        #: These are the only numbers in this file that describe the actual link
        #: rather than an assumption about it. `outbound_backlog()` sees the local
        #: socket buffer and stops at the kernel; it cannot observe a serial cable,
        #: a bridge queue, or a PlayStation's 8-byte RX FIFO. The console can.
        self.reported_goodput = 0
        self.reported_rtt_millis = 0

        #: Snapshots dropped by should_send_snapshot(). A steadily climbing count
        #: is normal on serial and means pacing is doing its job; zero on a serial
        #: peer that is visibly behind means the backlog probe is not working.
        self.snapshots_skipped = 0

        #: Send every Nth snapshot to this peer, ADAPTED from what the peer
        #: reports rather than guessed from what kind of transport it is.
        #:
        #: This used to read `SERIAL_SNAPSHOT_DIVISOR if adapter.name == "raw"
        #: else 1` - the rate was decided by the adapter's name. That is the same
        #: species of mistake as every other constant this project has been bitten
        #: by: a number standing in for a measurement nobody was taking.
        self.snapshot_divisor = PACING_INITIAL_DIVISOR
        self._snapshot_tick = 0

        #: The wire's own rate, which is the one congestion fact the server knows
        #: without being told. See ByteBudget for why a feed-forward limit is
        #: needed alongside the two feedback ones.
        #:
        #: Chosen by adapter because the adapter IS the transport: the raw port is
        #: reached down a serial cable through the bridge, the protobuf port by an
        #: emulator over TCP. That is a fact about the physical link, not the
        #: "number standing in for a measurement" that the divisor comment warns
        #: about -- there is no measurement of a cable's baud to take, it is set at
        #: both ends by configuration and must simply be respected.
        self.budget = ByteBudget(
            SERIAL_BYTES_PER_SECOND if adapter.name == "raw" else 0,
            time.monotonic(),
        )

        #: The lowest round trip this peer has ever reported, i.e. the link's own
        #: propagation delay with no queue in it. Everything above this is queue.
        self.base_rtt_millis = 0

        # Optional game-specific AppData handler: (session, payload) -> handled.
        # Returning False (or leaving it unset) relays the payload to the room.
        self.on_app_data: Callable[["Session", bytes], bool] | None = None

        # Called when a reliable message is abandoned after exhausting its retries.
        # The game gets a chance to regenerate it from current state rather than
        # let it vanish - the room roster is the message this exists for.
        self.on_reliable_dropped: Callable[["Session", bytes], None] | None = None

    # --- outbound -------------------------------------------------------

    def send_frame(self, packet_type: PacketType, payload: bytes = b"", *, seq: int | None = None) -> None:
        if seq is None:
            seq = self._tx_seq
            self._tx_seq = (self._tx_seq + 1) & 0xFFFF
        frame = encode_frame(packet_type, payload, src_slot=HOST_SLOT, seq=seq)
        self.send_raw(frame)

    def send_raw(self, psnl_bytes: bytes) -> None:
        """Write pre-encoded PSNL bytes, letting the adapter wrap them.

        Taking pre-encoded bytes is what lets a room serialize its snapshot once
        and hand the identical buffer to every member.
        """
        # Charged whatever it is, so the budget describes the whole offered load
        # rather than only the part that consults it. A reliable message is never
        # refused here - it still goes - but the debt it leaves is what makes the
        # next snapshot wait for the wire instead of piling on top of it.
        self.budget.charge(len(psnl_bytes))
        try:
            self._writer.write(self._adapter.encode(psnl_bytes))
        except (ConnectionError, RuntimeError):
            pass  # the reader task owns teardown

    def outbound_backlog(self) -> int:
        """Bytes queued for this peer that the OS has not accepted yet.

        `StreamWriter.write()` never blocks and never refuses, so on a slow link
        this is the only evidence that we are producing faster than the peer can
        consume. It is the signal the whole pacing scheme runs on: the bridge stops
        reading its socket when its serial queue fills, TCP closes the window, and
        the backlog shows up here.
        """
        transport = self._writer.transport
        try:
            return transport.get_write_buffer_size()
        except (AttributeError, NotImplementedError):
            # Test doubles and some transports do not implement it. Reporting zero
            # means "no evidence of backlog", which keeps pacing off rather than
            # throttling a link we cannot measure.
            return 0

    def wants_snapshot(self) -> bool:
        """Rate-limit AND congestion-check, in that order.

        The rate limit is now ADAPTIVE (see `_adapt_pacing`), driven by the round
        trip the console reports. It exists because the congestion check below
        provably cannot see the failure that matters most on a serial console:
        `outbound_backlog()` reads the PC's socket buffer, but the loss happens in
        the PlayStation's 8-byte hardware RX FIFO - bytes arriving faster than its
        interrupt handler runs are dropped inside the console, and every counter on
        this side still reads perfectly healthy. A backpressure signal that stops
        at the cable cannot protect what is past the cable.

        The backlog check stays as a local backstop for a genuinely wedged socket,
        but it is no longer the policy. The policy is what the far end measured.
        """
        self._snapshot_tick += 1
        if self._snapshot_tick % self.snapshot_divisor != 0:
            return False
        return self.should_send_snapshot()

    def should_send_snapshot(self) -> bool:
        """Whether this session can afford a snapshot on this tick.

        Snapshots are latest-wins on both ends - the console overwrites its staged
        one (networkmanager.cpp onSnapshot) - so skipping one costs a fraction of a
        second of smoothing. A dropped *roster* costs the whole round: it is the
        largest message on the wire and the lobby cannot proceed without it.

        So when the link is congested we spend it on reliable traffic and let
        position updates go. Fast peers (Redux over TCP) never reach the watermark
        and are unaffected.
        """
        # THE WIRE FIRST, before any of the feedback signals below.
        #
        # This is the only check that works when the link is already drowning: the
        # two after it need the far end to be reachable in order to report that it
        # is not. Sized from the cable's own rate, so it holds whether or not a
        # single Ping ever gets back. See ByteBudget.
        #
        # A snapshot is the right thing to refuse: both ends treat them as
        # latest-wins (the console overwrites its staged one in
        # networkmanager.cpp onSnapshot), so a skipped one costs a fraction of a
        # second of smoothing, where a delayed roster costs the round.
        self.budget.refill(time.monotonic())
        if not self.budget.can_afford(SNAPSHOT_BUDGET_ESTIMATE):
            self.budget.refused += 1
            self.snapshots_skipped += 1
            return False

        backlog = self.outbound_backlog()
        if backlog >= SNAPSHOT_BACKLOG_LIMIT:
            self.snapshots_skipped += 1
            return False
        # Even a modest backlog means the wire is contended; if a reliable message
        # is waiting on that wire, it goes first. Without this the roster queues
        # behind snapshots that will be thrown away on arrival.
        if self._inflight is not None and backlog >= RELIABLE_PRIORITY_BACKLOG:
            self.snapshots_skipped += 1
            return False
        return True

    # --- inbound --------------------------------------------------------

    def feed(self, data: bytes) -> None:
        # Anything at all counts as a sign of life, including bytes that do not
        # form a whole frame yet. A console that is talking is a console that is
        # there, and judging liveness on complete frames would penalise a slow
        # serial link for being slow.
        self.last_rx_at = time.monotonic()
        for frame in self._parser.feed(self._adapter.decode(data)):
            self._dispatch(frame)

    def is_stale(self, now: float, timeout: float) -> bool:
        """Has this peer stopped talking for longer than a live console ever
        would?

        This exists because of one specific failure that a TCP server CANNOT see
        any other way. Real hardware reaches this server through a bridge
        process: the PlayStation speaks serial to a PC, and the PC holds the TCP
        connection. Reset the console mid-game and the SOCKET IS STILL OPEN - the
        bridge is a different process and has not gone anywhere. So there is no
        disconnect, `on_disconnect` never runs, and the room keeps a member that
        no longer exists.

        The consequence is not cosmetic. Reset every console in a room and the
        room stays full of ghosts forever: it never empties, so
        `destroy_if_empty` never fires, and it is still listed and still
        joinable. Anyone who joins it waits for players who are not coming.

        A live console cannot be silent for long. The engine sends a snapshot
        every two frames while replicating and a Ping on its own clock, and the
        server's reliable retransmits draw an EVENT_ACK out of anything that is
        listening - so several seconds of complete silence is not a quiet moment,
        it is an absence.
        """
        return (now - self.last_rx_at) > timeout

    def _dispatch(self, frame: Frame) -> None:
        match frame.type:
            case PacketType.HELLO:
                self._on_hello(frame)
            case PacketType.SNAPSHOT:
                self._on_snapshot(frame)
            case PacketType.EVENT | PacketType.OBJECT_STATE | PacketType.APP_DATA:
                self._on_reliable(frame)
            case PacketType.EVENT_ACK:
                # The console has always sent these; the server used to drop them
                # on the floor, which is what made server -> console delivery
                # unreliable in the first place.
                self._on_event_ack(frame)
            case PacketType.PING:
                self._on_ping(frame)
            case PacketType.BYE:
                self.close()
            case _:
                pass  # unknown types are ignored, matching the console's dispatch

    def _on_hello(self, frame: Frame) -> None:
        try:
            hello = HelloPayload.unpack(frame.payload)
        except Exception:
            logger.warning("%s: malformed Hello", self.peer)
            return

        if hello.magic != MAGIC:
            logger.warning("%s: foreign Hello magic %#x", self.peer, hello.magic)
            return
        if hello.proto_version != PROTO_VERSION:
            logger.warning("%s: proto v%d != v%d", self.peer, hello.proto_version, PROTO_VERSION)
            return  # the console latches VersionMismatch on its own

        rebind = bool(frame.header.flags & FLAG_REBIND)

        if self.connected:
            # A second Hello on a live session is either a lost-ack retry or a
            # scene change, and the same response is correct for both: record
            # where the console now says it is, and re-ack. Re-acking the same
            # scene is a no-op; re-acking a new one completes the handover.
            #
            # This deliberately does NOT require the rebind flag. The console
            # only sets that flag when it already knows its slot
            # (networkmanager.cpp sendHello), so a console that sat in the lobby
            # without one CANNOT set it - and onHelloAck rejects any ack whose
            # sceneHash differs from its own, latching SceneMismatch. Keying off
            # the flag would therefore kill exactly the lobby -> game handover
            # this exists to support.
            #
            # A rebind is never rejected for scene mismatch either. During a
            # handover the room is briefly split across two scenes because
            # consoles finish loading at different times, and whoever rebinds
            # first would otherwise be refused for disagreeing with the scene
            # everyone is in the process of leaving. Snapshots group per scene.
            self.scene_hash = hello.scene_hash
            # The console restarts its reliable sequence at 1 on every
            # Net.Connect() (NetLink::reset), so the numbering we remember from
            # before the scene change is stale. Forgetting it here is what stops
            # the FIRST packet of the new scene being mistaken for a duplicate of
            # the last packet of the old one - which _on_reliable would ACK and
            # then silently discard, leaving the console certain it had been
            # heard and the server never acting on it.
            self._reliable_rx_seq = None
            if self.room is not None and self.member is not None:
                self.room.rebind(self.member, hello.scene_hash)
                logger.info(
                    "%s: rebound slot %d to scene %#010x%s",
                    self.peer, self.slot, hello.scene_hash, "" if rebind else " (no flag)",
                )
            self._send_ack()
            return

        self.scene_hash = hello.scene_hash
        if self._auto_join and self.room is None:
            if not self._join_default_room(hello.scene_hash):
                return

        self.connected = True
        self._send_ack()
        logger.info(
            "%s: connected slot=%d scene=%#010x via %s",
            self.peer, self.slot, hello.scene_hash, self._adapter.name,
        )
        if self._on_connect is not None:
            self._on_connect(self)

    def _join_default_room(self, scene_hash: int) -> bool:
        """Put a console straight into a playable room.

        Lobby/room selection is a PSXSUS concern that rides the reliable channel;
        until that exists, and for tests, a console that says Hello lands in the
        first room that fits it.
        """
        for room in self._rooms.all_rooms():
            if room.is_full or room.private:
                continue
            if room.scene_hash is not None and room.scene_hash != scene_hash:
                continue
            if self._try_join(room, scene_hash):
                return True
        room = self._rooms.create(name="default")
        return self._try_join(room, scene_hash)

    def _try_join(self, room: Room, scene_hash: int) -> bool:
        try:
            member = room.add(self, scene_hash)
        except (RoomFullError, SceneMismatchError) as exc:
            logger.info("%s: join rejected: %s", self.peer, exc)
            return False
        self.room = room
        self.member = member
        self.slot = member.slot
        return True

    # --- membership, for a game that owns its own lobby ------------------

    def join_room(self, room: Room, name: str = "") -> Member:
        """Put this session in a room and give it a slot.

        Raises RoomFullError / SceneMismatchError, which the caller is expected
        to turn into whatever its protocol says "no" looks like.
        """
        if self.room is not None:
            self.leave_room()
        member = room.add(self, self.scene_hash, name=name)
        self.room = room
        self.member = member
        self.slot = member.slot
        # The console learns its slot from a HelloAck, so re-ack now: otherwise
        # it would still believe it has no slot until the next scene load.
        self._send_ack()
        return member

    def leave_room(self) -> None:
        if self.room is None:
            return
        room, self.room = self.room, None
        room.remove(self.slot)
        self._rooms.destroy_if_empty(room)
        self.member = None
        self.slot = NO_SLOT

    def _send_ack(self) -> None:
        room = self.room
        # Report the players this console will actually SEE - i.e. those in the
        # same scene - rather than everyone in the room. During a handover the
        # two differ, and the console uses this to size its expectations.
        if room is not None and self.member is not None:
            visible = len(room.scene_groups().get(self.member.scene_hash, []))
        else:
            visible = 1
        ack = HelloAckPayload(
            your_slot=self.slot,
            host_slot=HOST_SLOT,
            scene_hash=self.scene_hash,
            player_count=visible,
        )
        self.send_frame(PacketType.HELLO_ACK, ack.pack())

    def _on_ping(self, frame: Frame) -> None:
        """Echo a Ping back as a Pong, and record what the console measured.

        This used to be `pass` - "liveness only: a valid frame is itself the
        signal" - and that single line is why the server knew nothing about the
        link it was writing to. Every pacing decision was based on a constant
        chosen from an assumption (a divisor keyed off the adapter's NAME) or on
        `get_write_buffer_size()`, which cannot see past the kernel socket buffer
        and certainly not to the far end of a serial cable.

        The console can see it. It measures its own throughput and round trip and
        reports them here, so the server can pace by what its peer actually
        observed rather than by what this file assumed. The echo must be returned
        unmodified: it is how the console times the round trip and rejects a stale
        reply.
        """
        try:
            ping = PingPayload.unpack(frame.payload)
        except Exception:
            # A malformed Ping is not worth dropping the session for; it is
            # diagnostic traffic, and the link is presumably already unwell.
            logger.debug("%s: malformed Ping (%d bytes)", self.peer, len(frame.payload))
            return

        self.reported_goodput = ping.goodput
        self.reported_rtt_millis = ping.rtt_millis
        self._adapt_pacing(ping.rtt_millis)

        reply = PingPayload(echo=ping.echo, goodput=0, rtt_millis=0)
        self.send_frame(PacketType.PONG, reply.pack())

    def _adapt_pacing(self, rtt_millis: int) -> None:
        """Set the snapshot rate from the queueing delay the peer is reporting.

        Delay-based congestion control, in the Vegas/LEDBAT sense: sending faster
        than a link drains does not fail, it QUEUES, and the queue appears as round
        trip above the link's own propagation delay long before anything is
        dropped. Rising RTT is therefore an early warning, where a loss counter is
        a post-mortem.

        It is also the only signal that works across this topology. The server's
        own view stops at its socket buffer; it cannot see the bridge's queue, the
        serial cable, or the PlayStation's 8-byte RX FIFO. The console sees all
        three, and its RTT is how it reports them.

        What this REPLACES is the important part: the send rate used to be chosen
        by asking whether the adapter was named "raw", with a comment justifying
        the number in terms of a 215-byte snapshot and a 115200 link. The snapshot
        is 127 bytes now and the link is 57600. Every such constant in this project
        has eventually been wrong, silently, in a way that looked like a game bug -
        so this one is measured instead.
        """
        if rtt_millis <= 0:
            return  # the console has no measurement yet; leave pacing alone

        # Track the baseline: the lowest round trip ever seen is the link with no
        # queue in it. Decay toward a new minimum rather than snapping to it, so a
        # single lucky sample cannot make every later reading look congested.
        if self.base_rtt_millis == 0 or rtt_millis < self.base_rtt_millis:
            if self.base_rtt_millis == 0:
                self.base_rtt_millis = rtt_millis
            else:
                self.base_rtt_millis -= max(
                    1, (self.base_rtt_millis - rtt_millis) // PACING_BASE_RTT_DECAY
                )

        queue_millis = rtt_millis - self.base_rtt_millis

        if queue_millis > PACING_TARGET_QUEUE_MILLIS:
            # Queues are building somewhere we cannot see. Send less often.
            self.snapshot_divisor = min(PACING_MAX_DIVISOR, self.snapshot_divisor + 1)
        elif queue_millis < PACING_RELIEF_QUEUE_MILLIS:
            # Comfortably clear. Open up, one step at a time.
            #
            # Asymmetric on purpose: backing off is immediate, opening up is
            # gradual. Being briefly too slow costs coarser remote movement; being
            # too fast costs a console that drops the room roster, which ends the
            # round. The two are not worth trading evenly.
            self.snapshot_divisor = max(PACING_MIN_DIVISOR, self.snapshot_divisor - 1)

    def _on_snapshot(self, frame: Frame) -> None:
        """A client reporting its own avatar. Client-authoritative by design."""
        if self.member is None or len(frame.payload) < 4:
            return
        avatar_count = frame.payload[0]
        off = 4
        for _ in range(avatar_count):
            if off + RECORD_SIZE > len(frame.payload):
                break
            slot, flags, _pad, px, py, pz, yaw = struct.unpack_from(
                "<BBHiiii", frame.payload, off
            )
            off += RECORD_SIZE
            # Trust a client only for its OWN slot. Without this a client could
            # drive every other player's avatar by simply claiming their slot.
            if slot != self.slot:
                continue
            self.member.avatar = Avatar(px=px, py=py, pz=pz, yaw=yaw, flags=flags)

    def _on_reliable(self, frame: Frame) -> None:
        """Reliable channel: ack first, then act on genuinely new packets.

        Acking duplicates as well as new packets is what lets the sender stop
        retransmitting - the console does the same (netlink.cpp:126).
        """
        self.send_frame(PacketType.EVENT_ACK, b"", seq=frame.seq)
        if self._reliable_rx_seq == frame.seq:
            return  # duplicate: already handled
        self._reliable_rx_seq = frame.seq

        if frame.type == PacketType.EVENT and len(frame.payload) >= 8:
            event_id, arg = struct.unpack_from("<ii", frame.payload, 0)
            self._on_game_event(event_id, arg)
        elif frame.type == PacketType.OBJECT_STATE:
            self._relay_to_room(frame)
        elif frame.type == PacketType.APP_DATA:
            self._on_app_data(frame)

    def _on_app_data(self, frame: Frame) -> None:
        """The game's own channel.

        The server is as incurious about these bytes as the engine is: a hook is
        provided for game-specific handling (rooms, roles, votes) and the default
        is to relay. Teaching the server PSXSUS's protocol here would be the same
        mistake as putting room packet types in netprotocol.hh.
        """
        if self.on_app_data is not None:
            handled = self.on_app_data(self, frame.payload)
            if handled:
                return
        self._relay_to_room(frame)

    def send_app_data(self, payload: bytes) -> None:
        """Send game data reliably: retransmitted until the console acknowledges.

        STOP-AND-WAIT: exactly one message is in flight, the rest queue behind it.
        That is not a simplification, it is a requirement of the receiver. The
        console dedupes inbound reliable frames against the LAST sequence number
        only - "stop-and-wait means at most one new seq at a time, so single-last
        dedup is exact" (netlink.cpp, handleIncomingReliable). Pipelining here
        would break that: send 5,6,7 then retransmit 5, and the console sees a seq
        that is not its last one and delivers the message a second time. For
        MeetingStart that means the vote screen reopening on its own.

        So the ordering constraint lives here, where it can be fixed without
        touching code that runs on hardware.
        """
        self._outbox.append(payload)
        if len(self._outbox) > RELIABLE_WINDOW:
            # The console is not acknowledging. Drop the OLDEST: it is the most
            # likely to be stale, and a console that recovers should receive
            # current state rather than replay a backlog.
            dropped = self._outbox.pop(0)
            logger.warning("%s: reliable backlog full, dropped %d bytes", self.peer, len(dropped))
        self._pump_outbox(None)

    def _pump_outbox(self, now: float | None) -> None:
        """Put the next queued message on the wire, if nothing is in flight."""
        if self._inflight is not None or not self._outbox:
            return
        payload = self._outbox.pop(0)
        seq = self._tx_seq
        self._tx_seq = (self._tx_seq + 1) & 0xFFFF
        self.send_frame(PacketType.APP_DATA, payload, seq=seq)
        # `due` is None when we have no clock to hand (send_app_data is called
        # from request handlers, not the tick loop). service_reliable stamps it on
        # its next pass, using the one clock that drives retries - mixing two time
        # sources is how you get a retry that fires immediately or never.
        self._inflight = [seq, payload, (now + RELIABLE_RETRY_SECONDS) if now else None, 0]

    def service_reliable(self, now: float) -> None:
        """Resend the in-flight message if its deadline passed; then send the next."""
        if self._inflight is not None:
            seq, payload, due, attempts = self._inflight
            if due is None:
                self._inflight[2] = now + RELIABLE_RETRY_SECONDS
            elif now >= due:
                if attempts >= RELIABLE_MAX_ATTEMPTS:
                    # Give up rather than resend forever. A console this
                    # unresponsive has a link problem repetition will not fix, and
                    # dropping it unblocks everything queued behind it.
                    #
                    # But "gave up" must not mean "lost silently". Tell the game, so
                    # anything it cannot afford to lose can be regenerated from
                    # current state - which is better than a resend anyway, since by
                    # now the payload is seconds stale.
                    logger.warning(
                        "%s: gave up on reliable seq %d after %d attempts (%d bytes)",
                        self.peer, seq, attempts, len(payload),
                    )
                    self._inflight = None
                    if self.on_reliable_dropped is not None:
                        try:
                            self.on_reliable_dropped(self, payload)
                        except Exception:
                            logger.exception("on_reliable_dropped failed")
                else:
                    self.send_frame(PacketType.APP_DATA, payload, seq=seq)
                    self._inflight[2] = now + self._retry_delay(attempts + 1)
                    self._inflight[3] = attempts + 1
        self._pump_outbox(now)

    def _retry_delay(self, attempts: int) -> float:
        """Backoff for the Nth resend, floored at what a reply can actually take.

        NEVER RETRANSMIT BEFORE THE ACK COULD HAVE ARRIVED. That is not a tuning
        preference, it is the difference between repairing a link and destroying
        one, and this function got it wrong in the way that matters.

        It was a flat RELIABLE_RETRY_SECONDS (0.30s) base, and 0.30s is far below
        the real round trip during play. The console drains its transmit ring at
        48 bytes per frame, so an acknowledgement can sit behind a full 1024-byte
        ring for ~700ms before it even reaches the wire. Every reliable message was
        therefore resent two or three times before its ack could POSSIBLY come
        back; each copy took wire away from the ack it was waiting for, and the
        link fed on itself. Measured: 66.2 KB sent to a console that had received
        an honest 6.7 KB's worth of capacity in the same window.

        So the floor now comes from the link itself:

          * the round trip the console REPORTED, when it has managed to report one
            (with margin, because a mean RTT is not a maximum); and
          * failing that -- and on a busy link it usually does fail, because the
            console cannot send a Ping when its ring is full -- from the wire rate
            and the size of the message actually in flight. A 250-byte roster on a
            5760 B/s cable is 43ms each way BEFORE any queueing, and the queue is
            the entire problem.

        The exponential backoff on top is unchanged and still does its job; this
        only stops the first resend from firing into a reply that is still in
        transit.
        """
        delay = RELIABLE_RETRY_SECONDS * (RELIABLE_RETRY_BACKOFF ** attempts)
        return max(min(delay, RELIABLE_RETRY_MAX_SECONDS), self._min_retry_delay())

    def _min_retry_delay(self) -> float:
        """The soonest a resend could possibly be useful, in seconds."""
        if self.reported_rtt_millis > 0:
            # A reported round trip is an average; the tail is what times out.
            return min(
                RELIABLE_RETRY_MAX_SECONDS,
                (self.reported_rtt_millis / 1000.0) * RELIABLE_RTT_MARGIN,
            )
        if not self.budget.metered:
            return 0.0
        # No measurement has ever arrived. Derive it: the message has to get there
        # and an ack has to come back, and the console's return path is far slower
        # than ours (48 bytes per frame against a whole cable).
        inflight = len(self._inflight[1]) if self._inflight else 0
        out_seconds = (inflight + FRAME_OVERHEAD_BYTES) / self.budget.rate
        return min(
            RELIABLE_RETRY_MAX_SECONDS,
            out_seconds + CONSOLE_ACK_LATENCY_SECONDS,
        )

    def _on_event_ack(self, frame: Frame) -> None:
        if self._inflight is not None and self._inflight[0] == frame.seq:
            self._inflight = None
            self._pump_outbox(None)

    @property
    def reliable_backlog(self) -> int:
        """Queued + in-flight reliable messages. Diagnostics and tests."""
        return len(self._outbox) + (1 if self._inflight else 0)

    def _on_game_event(self, event_id: int, arg: int) -> None:
        logger.debug("%s: event id=%d arg=%d", self.peer, event_id, arg)
        if self.room is None:
            return
        payload = struct.pack("<ii", event_id, arg)
        for member in self.room.members.values():
            if member.session is not self:
                member.session.send_frame(PacketType.EVENT, payload)

    def _relay_to_room(self, frame: Frame) -> None:
        if self.room is None:
            return
        for member in self.room.members.values():
            if member.session is not self:
                member.session.send_frame(frame.type, frame.payload)

    # --- teardown -------------------------------------------------------

    def close(self) -> None:
        if self.room is not None and self.slot != NO_SLOT:
            self.room.remove(self.slot)
            self._rooms.destroy_if_empty(self.room)
            self.room = None
            self.member = None
        self.connected = False
        try:
            self._writer.close()
        except Exception:
            pass
