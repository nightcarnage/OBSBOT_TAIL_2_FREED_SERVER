#!/usr/bin/env python3
"""The Tail 2 FreeD fan-out server.

One small process between the camera and its consumers:

    OBSBOT Tail 2 --(FreeD D1, UDP)--> THIS SERVER :40100
                                         |-- raw D1 ----------> Unreal LiveLink FreeD (:40000)
                                         |-- ASCII pose ------> the OBSBOT MCP's eyes (:40101)
                                         |-- raw D1 ----------> any --extra targets
                                         '-- HTTP /pose ------> anything that speaks curl (:40110)

The camera keeps exactly one FreeD destination (its own persistent setting --
see tools/set_freed_target.py), so whoever owns that port owns the data. This
server owns it and hands the data to everyone else, inside or outside the
engine:

  * inside the engine  -- raw D1 packets are re-sent verbatim, so a stock
                          LiveLink FreeD source on UDP 40000 sees exactly what
                          it would have seen coming straight from the camera;
  * outside the engine -- the same samples are re-encoded as the one-line
                          ASCII telemetry format the OBSBOT MCP server parses
                          (yaw, pitch, roll, focal, focus, x, y, z), and
                          published as JSON on an HTTP port.

Stdlib only, Python 3.8+. Windows-friendly (no dependencies, no services).
"""

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D1_SIZE = 29

DEFAULT_LISTEN = 40100
DEFAULT_LIVELINK = "127.0.0.1:40000"
DEFAULT_TAP = "127.0.0.1:40101"
DEFAULT_HTTP_PORT = 40110


# -----------------------------------------------------------------------------
# the packet
# -----------------------------------------------------------------------------

def read_signed24(buf, offset):
    value = (buf[offset] << 16) | (buf[offset + 1] << 8) | buf[offset + 2]
    if value & 0x800000:
        value |= ~0xFFFFFF
    return value


def read_unsigned24(buf, offset):
    return (buf[offset] << 16) | (buf[offset + 1] << 8) | buf[offset + 2]


def checksum_ok(buf):
    """FreeD D1: the last byte is 0x40 minus the byte sum of the rest."""
    return (0x40 - sum(buf[:0x1C])) & 0xFF == buf[0x1C]


def parse_d1(buf):
    """A 29-byte FreeD D1 packet -> a dict, or None if it is not one."""
    if len(buf) < D1_SIZE or buf[0] != 0xD1:
        return None
    return {
        "yaw": read_signed24(buf, 0x02) / 32768.0,
        "pitch": read_signed24(buf, 0x05) / 32768.0,
        "roll": read_signed24(buf, 0x08) / 32768.0,
        # FreeD crosses X and Y on the wire: 0x0B carries Y, 0x0E carries X.
        "y": read_signed24(buf, 0x0B) / 640.0,
        "x": read_signed24(buf, 0x0E) / 640.0,
        "z": read_signed24(buf, 0x11) / 640.0,
        "focal": read_unsigned24(buf, 0x14),
        "focus": read_unsigned24(buf, 0x17),
        "camera_id": buf[1],
        "checksum": checksum_ok(buf),
    }


def format_tap_line(sample):
    """The OBSBOT MCP's telemetry format, byte for byte (it splits on commas).

    yaw,<f>,pitch,<f>,roll,<f>,focal,<i>,focus,<i>,x,<f>,y,<f>,z,<f>,pts,<i>
    """
    return (
        "yaw,%.4f,pitch,%.4f,roll,%.4f,focal,%d,focus,%d,x,%.4f,y,%.4f,z,%.4f,pts,0\n"
        % (sample["yaw"], sample["pitch"], sample["roll"],
           sample["focal"], sample["focus"],
           sample["x"], sample["y"], sample["z"])
    )


def addr_of(text):
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST:PORT, got %r" % text)
    return (host, int(port))


def addr_str(addr):
    return "%s:%d" % (addr[0], addr[1])


# -----------------------------------------------------------------------------
# state + http
# -----------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None
        self.received = 0
        self.bad = 0
        self.bad_checksum = 0
        self.sent = {}


def start_http(state, host, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.rstrip("/") or "/"
            if path == "/health":
                body, ctype = b"ok\n", "text/plain"
            elif path == "/pose":
                with state.lock:
                    sample = dict(state.latest) if state.latest else None
                    received, bad = state.received, state.bad
                if sample is None:
                    payload = {"ok": False, "error": "no FreeD samples yet",
                               "packets": received}
                else:
                    age = time.time() - sample.pop("rx")
                    sample.pop("checksum", None)
                    payload = dict(sample, ok=True, age_s=round(age, 3),
                                   packets=received, bad=bad)
                body = json.dumps(payload).encode("utf-8")
                ctype = "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever,
                              name="freed-http", daemon=True)
    thread.start()
    return server


# -----------------------------------------------------------------------------
# main loop
# -----------------------------------------------------------------------------

def run(args):
    targets = [("livelink", args.livelink, "raw")]
    if args.tap is not None:
        targets.append(("tap", args.tap, "ascii"))
    for index, extra in enumerate(args.extra):
        targets.append(("extra%d" % (index + 1), extra, "raw"))

    state = State()

    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No SO_REUSEADDR on purpose: a second server must fail loudly, not
        # silently steal nothing while pretending to work.
        recv.bind(("0.0.0.0", args.listen))
    except OSError as exc:
        print("cannot bind UDP %d: %s" % (args.listen, exc))
        print("(another FreeD server, or an old engine tap, may hold the port)")
        return 1
    recv.settimeout(1.0)

    send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    http = None
    if args.http_port:
        http = start_http(state, args.http_bind, args.http_port)

    print("listening on 0.0.0.0:%d" % args.listen)
    for name, addr, mode in targets:
        print("  -> %-8s %-22s %s" % (name, addr_str(addr),
                                      "raw D1" if mode == "raw" else "ASCII pose"))
    if http is not None:
        print("  -> http     http://%s:%d/pose" % (args.http_bind, args.http_port))
    print("waiting for the camera (it must be pointed at this port -- "
          "tools/set_freed_target.py --target-port %d)" % args.listen)

    last_stats = time.time()
    first_peer = None
    interrupted = False

    try:
        while True:
            try:
                data, peer = recv.recvfrom(2048)
            except socket.timeout:
                data = None
            except OSError as exc:
                print("receive failed: %s" % exc)
                break

            if data is not None:
                now = time.time()
                sample = parse_d1(data)

                if sample is None:
                    state.bad += 1
                else:
                    if not sample["checksum"]:
                        state.bad_checksum += 1
                    if state.received == 0:
                        first_peer = peer
                        print("first D1 packet from %s:%d" % peer, flush=True)
                    state.received += 1

                    with state.lock:
                        state.latest = dict(sample, rx=now)

                    for name, addr, mode in targets:
                        payload = (data if mode == "raw"
                                   else format_tap_line(sample).encode("ascii"))
                        try:
                            send.sendto(payload, addr)
                            state.sent[name] = state.sent.get(name, 0) + 1
                        except OSError as exc:
                            print("send to %s (%s) failed: %s"
                                  % (name, addr_str(addr), exc), flush=True)

            if args.stats_seconds and time.time() - last_stats >= args.stats_seconds:
                last_stats = time.time()
                line = "d1: %d packets (%d bad, %d bad checksum)" % (
                    state.received, state.bad, state.bad_checksum)
                if state.latest:
                    sample = state.latest
                    age = time.time() - sample["rx"]
                    line += " | yaw %+.2f pitch %+.2f focal %d" % (
                        sample["yaw"], sample["pitch"], sample["focal"])
                    if age > 3.0:
                        line += " (last %.1fs ago!)" % age
                if state.sent:
                    line += " | " + " ".join(
                        "%s=%d" % item for item in sorted(state.sent.items()))
                print(line, flush=True)

    except KeyboardInterrupt:
        interrupted = True
    finally:
        recv.close()
        send.close()
        if http is not None:
            http.shutdown()

    print("bye: %d packets (%d bad), sends: %s"
          % (state.received, state.bad, state.sent))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Tail 2 FreeD fan-out: camera D1 in, LiveLink + ASCII + HTTP out")
    parser.add_argument("--listen", type=int, default=DEFAULT_LISTEN,
                        help="UDP port the camera sends to (default: %(default)s)")
    parser.add_argument("--livelink", type=addr_of, default=addr_of(DEFAULT_LIVELINK),
                        help="raw-D1 target for Unreal's LiveLink FreeD source "
                             "(default: %(default)s)")
    parser.add_argument("--tap", type=addr_of, default=addr_of(DEFAULT_TAP),
                        help="ASCII-pose target for the OBSBOT MCP (default: %(default)s)")
    parser.add_argument("--no-tap", action="store_true",
                        help="disable the ASCII pose tap")
    parser.add_argument("--extra", type=addr_of, action="append", default=[],
                        help="additional raw-D1 targets (repeatable)")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                        help="JSON /pose port, 0 disables (default: %(default)s)")
    parser.add_argument("--http-bind", default="127.0.0.1",
                        help="interface for the JSON endpoint (default: %(default)s)")
    parser.add_argument("--stats-seconds", type=float, default=5.0,
                        help="status line interval, 0 to silence (default: %(default)s)")
    args = parser.parse_args(argv)

    if args.no_tap:
        args.tap = None

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
