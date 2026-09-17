#!/usr/bin/env python3
"""Tail 2 camera comfort tools: go home, read and set zoom.

The camera's home button lives at POST /camera/sdk/ptz/reset -- a route that
does NOT appear in the vendor's RESTful HTML document. It was found in the
camera's own shipped web app (ControlPage-*.js), right next to /ptz/preset and
/ptz/rollbias, and it is what the UI's recenter button calls.

Usage:
    python camera_home.py                 # bring the camera back to zero
    python camera_home.py --status        # just read the zoom state
    python camera_home.py --zoom 2.0      # set zoom ratio (speed 3)

Watch the result in the FreeD stream: after a reset, yaw/pitch settle to ~0.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_CAMERA = "192.168.15.236"


def call(base, method, path, payload=None, timeout=10.0):
    """One REST call -> (status, parsed-or-text body). Failures are values."""
    url = base + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, "%s: %s" % (type(exc).__name__, exc)

    try:
        return status, json.loads(body) if body else None
    except ValueError:
        return status, body


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--camera", default=DEFAULT_CAMERA,
                        help="camera IP (default: %(default)s)")
    parser.add_argument("--port", type=int, default=80,
                        help="camera HTTP port (default: %(default)s)")
    parser.add_argument("--status", action="store_true",
                        help="only read the zoom state, move nothing")
    parser.add_argument("--zoom", type=float,
                        help="set the zoom ratio instead of going home (e.g. 2.0)")
    parser.add_argument("--zoom-speed", type=int, default=3,
                        help="zoom speed 1-4 (default: %(default)s)")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="seconds to wait after a move before reporting "
                             "(default: %(default)s)")
    args = parser.parse_args()

    base = "http://%s:%d/camera/sdk" % (args.camera, args.port)

    if args.status:
        status, body = call(base, "GET", "/ptz/zoom")
        print("zoom [%s]: %s" % (status, json.dumps(body)))
        return 0 if status == 200 else 1

    if args.zoom is not None:
        status, body = call(base, "PUT", "/ptz/zoom",
                            {"ratio": float(args.zoom), "speed": args.zoom_speed})
        print("zoom -> %sx [%s]: %s" % (args.zoom, status, json.dumps(body)))
        return 0 if status == 200 else 1

    print("bringing the camera home (POST /ptz/reset) ...")
    status, body = call(base, "POST", "/ptz/reset", {})
    print("reset [%s]: %s" % (status, json.dumps(body)))
    if status != 200:
        print("!! the camera refused the reset; check --camera / --port")
        return 1

    time.sleep(args.settle)
    status, body = call(base, "GET", "/ptz/zoom")
    print("zoom now: %s" % json.dumps(body))
    print("home sent. The FreeD stream (server /pose or the MCP) will show "
          "yaw/pitch settling to ~0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
