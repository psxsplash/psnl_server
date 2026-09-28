# PSXSUS room protocol

The game's own protocol, carried by the engine's **AppData** channel
(`Net.SendData` / `onNetData`). The engine assigns these bytes no meaning; the
server relays them by default and interprets only what it must own.

**Why it lives here and not in `netprotocol.hh`:** rooms, roles and votes are
*this game's* concepts. Adding `CreateRoom`/`JoinRoom` packet types to the engine
would bake one game's lobby model into psxsplash forever, and the next game built
on the engine would need its own. One opaque channel serves every game.

## Constraints that shape it

These are not style choices; each one is a hard limit measured elsewhere:

| Constraint | Value | Source |
|---|---|---|
| Max payload per message | **256 B** | `net::c_maxEventPayload` |
| Reliable channel | **stop-and-wait** | one packet in flight; ~30 msg/s per link |
| Reliable queue | **16** | shared with Event + ObjectState |
| Player name | **12 chars** | so a 15-room page fits one packet |
| Snapshot budget | 215 B / 2 frames | ~56% of 115200 8N1 |

Stop-and-wait is the reason every reply is designed to fit **one packet**. A
15-room list as 15 messages would cost 15 round trips (~500 ms); as one message
it costs one (~33 ms).

## Framing

Every message starts with a one-byte subtype. All integers little-endian, to
match the engine's wire and the PS1's byte order.

```
[u8 subtype][payload...]
```

### Client -> server

| # | Name | Payload | Notes |
|---|---|---|---|
| 0x01 | `SetName` | `char name[12]` | NUL-padded. Server truncates to 12. |
| 0x02 | `ListRooms` | `u8 page` | Asks for public rooms. |
| 0x03 | `CreateRoom` | `char name[12]`, `u8 flags`, `u8 mapId` | `flags` bit0 = private. `mapId` clamped mod MAP_COUNT. |
| 0x04 | `JoinRoom` | `u16 roomId` | Public rooms. |
| 0x05 | `JoinByCode` | `char code[4]` | Private rooms. |
| 0x06 | `LeaveRoom` | - | |
| 0x07 | `StartGame` | - | Room owner only; server validates. |
| 0x10 | `TaskComplete` | `u8 taskId` | Server validates and tallies. |
| 0x11 | `ReportBody` | `u8 slot` | Starts a meeting. |
| 0x12 | `Vote` | `u8 slot` | `0xFF` = skip. |
| 0x13 | `Kill` | `u8 slot` | Server validates role + cooldown. |
| 0x14 | `Sabotage` | `u8 kind` | Impostor only. |
| 0x15 | `FixSabotage` | `u8 kind` | |
| 0x16 | `UseVent` | `u8 ventId` | |
| 0x17 | `CallMeeting` | - | Emergency button. |
| 0x18 | `VentState` | `u8 entered` | Entering/leaving a vent, as opposed to `UseVent`'s hop between them. Relayed to the room. |
| 0x19 | `RequestState` | - | "Tell me the roster." A scene load wipes the console's Lua state, and the roster is only broadcast when it CHANGES. |
| 0x1A | `ReturnToLobby` | - | Owner only. Takes the WHOLE room back, so a rematch does not wait for the slowest player. |
| 0x1B | `SetMap` | `u8 mapId` | Owner only, and refused while a round is running. Broadcast back as `MapChanged`. |

### Server -> client

| # | Name | Payload |
|---|---|---|
| 0x81 | `RoomList` | `u8 page`, `u8 pageCount`, `u8 count`, `Room[count]` |
| 0x82 | `RoomJoined` | `u16 roomId`, `u8 yourSlot`, `char code[4]` |
| 0x83 | `RoomRejected` | `u8 reason` (1=full, 2=not found, 3=in progress, 4=scene mismatch, 5=too few players) |
| 0x84 | `RoomState` | `u8 playerCount`, `u8 ownerSlot`, `Player[playerCount]` |
| 0x85 | `GameStarting` | `u8 countdownFrames`, `u8 mapId` |
| 0x86 | `RoleAssigned` | `u8 role` (0=crew, 1=impostor), `u8 impostorCount` |
| 0x87 | `TaskList` | `u8 count`, `u8 taskIds[count]` |
| 0x88 | `TaskProgress` | `u8 done`, `u8 total` |
| 0x89 | `MeetingStart` | `u8 reason` (0=report, 1=button), `u8 bySlot`, `u8 bodySlot` |
| 0x8A | `VoteTally` | `u8 count`, `(u8 slot, u8 votes)[count]`, `u8 voters`, `(u8 voter, u8 votedFor)[voters]` | The second section is APPENDED, so a console built before it reads `count` pairs and stops. `votedFor` may be `0xFF` (skip). It is what the results screen draws a face beside each candidate from. |
| 0x8B | `Ejected` | `u8 slot` (`0xFF` = nobody), `u8 wasImpostor` |
| 0x8C | `SabotageStart` | `u8 kind`, `u16 timerFrames` |
| 0x8D | `SabotageEnd` | `u8 kind` |
| 0x8E | `PlayerDied` | `u8 slot` |
| 0x8F | `GameOver` | `u8 winner` (0=crew, 1=impostors) |
| 0x90 | `PlayerVent` | `u8 slot`, `u8 entered` |
| 0x91 | `SabotageTick` | `u8 kind`, `u16 framesLeft` |
| 0x92 | `MeetingEnd` | - |
| 0x93 | `ReturnLobby` | - | The answer to `ReturnToLobby`. Every console obeys it, including the owner's. |
| 0x94 | `MeetingTick` | `u8 votingOpen`, `u16 framesLeft` |
| 0x95 | `CooldownTick` | `u8 killSeconds`, `u8 meetingSeconds`, `u8 meetingsLeft` | Addressed to ONE player: a kill cooldown is personal. |
| 0x96 | `MapChanged` | `u8 mapId` | Broadcast when the owner changes the ship, and sent to a joining console right after `RoomJoined` - which is the only way a console that joined BY CODE learns the map. |
| 0x97 | `ImpostorList` | `u8 count`, `u8 slots[count]` | **Sent only to impostors, never broadcast.** Includes the recipient's own slot, which makes the message identical for every impostor so the server serialises it once. |

**`ImpostorList` is the one message whose secrecy is structural.** A crewmate is
never sent it, so a modified client has nothing to reveal - which is why the
console draws its partner marker with no "am I an impostor" check anywhere.
Guarding a broadcast with a role test would have left the round one edited byte
away from being visible to everybody. It is the same reason `RoleAssigned` tells
each player only their OWN role.

Both `RoleAssigned` and `ImpostorList` are also re-sent in reply to
`RequestState`: `Scene.Load` wipes the console's Lua state, and a console that
reloads mid-round would otherwise never learn either again.

### Records

```
Room (16 B)                       Player (16 B)
  u16 roomId                        u8  slot
  u8  playerCount                   u8  flags   bit0=alive bit1=owner
  u8  flags   bit0=private            char name[12]
              bit1=inProgress       u8  colour
              bit2..3=mapId
  char name[12]                     u8  reserved
```

`4 + 15 * 16 = 244 <= 256` - a full page of rooms is exactly one packet, with
12 bytes to spare. That is where the 12-character name limit comes from; it is
not an aesthetic choice.

## Authority

Split deliberately, and not negotiable:

- **Client-authoritative:** its own avatar transform. The console sends its own
  position each net tick and the server relays it. The server does *not*
  simulate - reimplementing PS1 collision and navigation in Python would mean
  two physics engines that must agree forever, and every divergence would be a
  desync bug with no natural home. The server validates only that a client
  claims *its own* slot.
- **Server-authoritative:** everything discrete - room membership, roles, task
  tallies, votes, kills, sabotage timers, win conditions. A client never decides
  it is an impostor, never decides a vote result, and never decides it killed
  someone.

Consequence, accepted knowingly: a modified client can teleport or move fast.
Server-side max-speed clamping is the hardening step once gameplay exists.

## Scene handover

The lobby and the game are different psxsplash scenes, and scene loads normally
destroy the network session. The sequence is:

1. Server sends `GameStarting`.
2. Each console calls `Net.SetPersistent(true)` then `Scene.Load(gameScene)`.
3. `clearScene()` drops the scene-scoped bindings but keeps the session.
4. The new scene calls `Net.Connect()`, which re-Hellos with `c_flagRebind` and
   reclaims the **same slot**.

Consoles finish loading at different times, so the room is briefly split across
two scenes. The server groups snapshots per scene, so players who have arrived
see each other immediately rather than waiting for the slowest console - and
nobody is rejected for "scene mismatch" mid-handover.
