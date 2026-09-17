# tail2-freed-server

One small, dependency-free Python process that takes the OBSBOT Tail 2's
**FreeD tracking data** and serves it to everyone — **inside the engine and
outside it**.

The camera keeps exactly **one** FreeD destination (a persistent camera
setting), so whoever owns that UDP port owns the data. This server owns it and
fans it out:

```
                       ┌──────────────────────────────┐
 OBSBOT Tail 2         │        THIS SERVER           │
   FreeD D1  ──────────▶  :40100  (camera sends here) │
   (~60 Hz, UDP)        │                              │
                       │  ├── raw D1  ────────────────┼──▶  127.0.0.1:40000   Unreal
                       │  │                           │     (stock LiveLink FreeD source)
                       │  ├── ASCII pose ─────────────┼──▶  127.0.0.1:40101   the OBSBOT MCP
                       │  │   yaw,pitch,roll,         │     (its "eyes" — orientation + zoom)
                       │  │   focal,focus,x,y,z       │
                       │  ├── raw D1  ────────────────┼──▶  any --extra targets
                       │  └── HTTP /pose ─────────────┼──▶  http://127.0.0.1:40110/pose
                       └──────────────────────────────┘     (JSON — curl from anywhere)
```

* **Inside the engine** — raw D1 packets are re-sent verbatim, so Unreal's
  stock **LiveLink FreeD** source (UDP 40000) sees exactly what it would have
  seen coming straight from the camera. Nothing custom is needed on the engine
  side; see the `Tail2_VP_Demo` project for a working rig.
* **Outside the engine** — the same samples are re-encoded as the one-line
  ASCII telemetry format the **OBSBOT MCP server** already parses (that is how
  its closed loop gets `freed_pose` / `freed_watch` / `ptz_goto` back), and
  published as JSON over HTTP for anything else.

## Quick start

```sh
# 1. Point the camera at this server (once; the camera remembers across reboots)
python tools/set_freed_target.py --camera 192.168.15.236 --target-port 40100

# 2. Run the server
python tail2_freed_server.py
#   (on Windows: double-click run_server.cmd instead -- same thing, keeps a
#    console open with the live stats)
#   listening on 0.0.0.0:40100
#     -> livelink 127.0.0.1:40000    raw D1
#     -> tap      127.0.0.1:40101    ASCII pose
#     -> http     http://127.0.0.1:40110/pose
#   first D1 packet from 192.168.15.236:49152
#   d1: 300 packets (0 bad, 0 bad checksum) | yaw +0.85 pitch -5.98 focal 1010 | ...
```

```sh
# 3. Read the pose from anywhere
curl http://127.0.0.1:40110/pose
# {"ok": true, "age_s": 0.002, "packets": 12345,
#  "yaw": 0.85, "pitch": -5.98, "roll": 0.0,
#  "focal": 1010, "focus": 16, "x": 0.0, "y": 0.0, "z": 0.0, "camera_id": 255}
```

## Ports

| Port  | Direction | Consumer |
|------:|-----------|----------|
| 40100 | in        | the camera sends its FreeD D1 here |
| 40000 | out       | Unreal LiveLink FreeD source (raw D1, verbatim) |
| 40101 | out       | OBSBOT MCP pose tap (ASCII line per sample) |
| 40110 | out       | HTTP JSON `/pose` (+ `/health`) |

Flags: `--listen`, `--livelink`, `--tap` / `--no-tap`, `--extra HOST:PORT`
(repeatable raw targets), `--http-port` (0 disables), `--http-bind`,
`--stats-seconds`.

## Camera home + zoom (`tools/`)

* `set_freed_target.py` — the one-time (persistent) camera configuration: the
  address and port its FreeD stream is sent to. Self-verifies by listening for
  the camera's own packets, so you know it worked before you leave the chair.
* `camera_home.py` — **bring the camera back to zero** and read/set zoom:

```sh
python tools/camera_home.py              # POST /camera/sdk/ptz/reset (go home)
python tools/camera_home.py --status     # GET /ptz/zoom
python tools/camera_home.py --zoom 2.0   # PUT /ptz/zoom {"ratio": 2.0, "speed": 3}
```

* `ptz_console.py` — the desktop console (copy on the Windows Desktop as
  `OBSBOT_TAIL_2 PTZ_CLOSED_LOOP.pyw`, with a `.bat` launcher): live pose
  readout (yaw/pitch, X/Y/Z, zoom/focal) from this server's `/pose` and **two
  roll rows** — `ROLL camera (FreeD)`: the head's own roll (this head has no
  roll axis, so it stays ~0.00 by design), and `ROLL lens`: the lens roll bias
  driven by Q/E (1° steps over ±10°). Also:
  hold-to-drive arrows (+ WASD), STOP, zoom presets and 0.1 steps, the
  **HOME** button, and an AI-tracking toggle that tells the truth — the camera
  answers `200` to `PUT /ai/workmode` even with nobody in view and keeps
  reporting `none` until it actually sees a person, so the button shows
  **off / waiting for a person / on** and goes green the moment tracking
  really engages. The mode value is `humanTrackingSingleMode` (the old
  script's `normalTrack` is not one of the camera's modes). Deploy or refresh
  the Desktop copies with `tools\deploy_console.cmd` — it writes both files
  there from this repo copy.

Home is `POST /camera/sdk/ptz/reset` — a route that **is not in the vendor's
RESTful HTML document**. It was found in the camera's own shipped web app
(`ControlPage-*.js`), where the recenter button calls it, next to
`/camera/sdk/ptz/preset` and `/ptz/rollbias`.

## The packet this speaks

Standard FreeD D1, 29 bytes, ~60 Hz:

| offset | bytes | meaning |
|-------:|------:|---------|
| 0x00   | 1     | `0xD1` packet type |
| 0x01   | 1     | camera id (the Tail 2 sends **255**) |
| 0x02   | 3     | yaw, signed 24-bit big-endian, 1/32768 degree |
| 0x05   | 3     | pitch, same scale |
| 0x08   | 3     | roll, same scale |
| 0x0B   | 3     | **Y** position, signed 24-bit, 1/64 mm (X and Y are crossed) |
| 0x0E   | 3     | **X** position |
| 0x11   | 3     | Z position |
| 0x14   | 3     | focal length encoder, unsigned 24-bit (zoom; 1010 ≈ 1×) |
| 0x17   | 3     | focus distance encoder, unsigned 24-bit |
| 0x1A   | 1     | user byte |
| 0x1B   | 1     | user byte |
| 0x1C   | 1     | checksum = `(0x40 - sum(bytes[0..0x1B])) & 0xFF` |

The Tail 2's own X/Y/Z carry **zero** (its position encoders are mount
offsets), so consumers that want a fixed rig height apply their own anchor —
the `Tail2_VP_Demo` level does this natively with
`TransformData → Use Location = off`.

## Notes

* **The server must be running for the data to flow anywhere.** The camera
  still sends to 40100; if nothing listens, nothing arrives. Make it a
  startup task (Task Scheduler, shortcut in the Startup folder) if the rig
  should always be live.
* Only one process can hold 40100. The server refuses to start if something
  else owns it (`SO_REUSEADDR` is deliberately not set).
* The ASCII tap format is exactly the engine's own telemetry format, so the
  OBSBOT MCP server needed no changes to get its eyes back:

  `yaw,<f>,pitch,<f>,roll,<f>,focal,<i>,focus,<i>,x,<f>,y,<f>,z,<f>,pts,<i>`

## Provenance

Recovered and proven across the OBSBOT_VP workspace: `FREED_PROTOCOL.md`
(two-mode FreeD on this camera: in-stream records *and* the camera's own D1
with the persistent 9999-configured destination), `M:\Tail2_FreeD\` (the
minimal receiver bundle this server generalizes), the `Tail2_VP_Demo` UE
project (stock LiveLink FreeD consumer), and the OBSBOT MCP (ASCII tap
consumer).
