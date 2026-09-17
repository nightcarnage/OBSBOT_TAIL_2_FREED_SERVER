#!/usr/bin/env python3
"""OBSBOT Tail 2 — PTZ console with a live FreeD readout (a real closed loop).

Their original controller fired commands blind. This one shows the camera's
actual pose while you drive it:

  * commands  -> camera REST (POST /ptz/gimbalcontrol speeds, PUT /ptz/zoom,
                 POST /ptz/reset for the home lever, PUT /ai/workmode)
  * feedback  <- the tail2-freed-server fan-out: GET http://127.0.0.1:40110/pose
                 (yaw/pitch/roll in degrees, x/y/z in cm, focal/focus
                  encoders — straight from the camera's own FreeD D1 stream)

Drive: hold the on-screen arrows, or W/A/S/D + arrow keys. The gimbal gets a
fresh speed command ~8x per second while held, and one stop when released.
Home: the big button (or H). Zoom: presets or the 0.1 steps (or Z / X).
Rolls: the panel shows two rows. ROLL camera (FreeD) is the head's own roll
from the camera's stream -- this head has no roll axis, so it stays ~0.00 by
design. ROLL lens is the lens roll bias Q / E step: image side, -10..+10
degrees in 1-degree steps (20 across the range). Quit: window close.

Stdlib only (tkinter + urllib). Close-loop needs the fan-out server running --
the status light says so plainly if it is not.
"""

import json
import queue
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request

CAMERA_IP = "192.168.15.236"
POSE_URL = "http://127.0.0.1:40110/pose"

HOLD_HZ = 8          # gimbal speed refreshes per second while a direction is held
POSE_HZ = 10         # pose polls per second
ZOOM_STEP = 0.1
ZOOM_MIN, ZOOM_MAX = 1.0, 4.0
ZOOM_SPEED = 3
ROLL_STEP = 1.0
ROLL_MIN, ROLL_MAX = -10.0, 10.0

BG, PANEL, PANEL2 = "#14171a", "#1e242b", "#262d36"
FG, DIM = "#e6edf3", "#8b98a5"
ACCENT, GOOD, WARN, BAD = "#4493f8", "#3fb950", "#d29922", "#f85149"
MONO = ("Consolas", 20, "bold")
MONO_S = ("Consolas", 11)
UI = ("Segoe UI", 9)


def rest(url, method="GET", payload=None, timeout=1.5):
    """One REST call -> (status, parsed-or-text body). Failures are values."""
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


class Console:
    def __init__(self, root):
        self.root = root
        self.camera = tk.StringVar(value=CAMERA_IP)
        self.pose_url = tk.StringVar(value=POSE_URL)
        self.speed = tk.IntVar(value=30)

        self.vector = {"pitch": 0, "yaw": 0}      # -1 / 0 / +1 per axis
        self.zoom_target = 1.0
        self.pose = None
        self.pose_error = None
        self.pose_wall = 0.0
        self.rate = 0.0
        self._last_count = None
        self._last_count_time = 0.0
        self._lock = threading.Lock()
        self._status_q = queue.Queue()
        self.mode_remote = None          # last /ai/workmode read-back
        self.track_request = None        # None = follow the camera; True/False = the user's wish
        self._track_request_time = 0.0
        self._track_state = None         # last shown state: off | waiting | on
        self.roll_bias = 0.0             # lens roll bias in degrees (-10..+10)
        self._roll_grace = 0.0

        self._build()
        threading.Thread(target=self._pose_loop, daemon=True).start()
        threading.Thread(target=self._drive_loop, daemon=True).start()
        threading.Thread(target=self._mode_loop, daemon=True).start()
        self.root.after(80, self._refresh)

    # ------------------------------------------------------------------ ui

    def _build(self):
        root = self.root
        root.title("OBSBOT Tail 2 — PTZ console (closed loop)")
        root.configure(bg=BG)
        root.resizable(False, False)

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(top, text="camera", bg=BG, fg=DIM, font=UI).grid(row=0, column=0, sticky="w")
        tk.Entry(top, textvariable=self.camera, width=16, bg=PANEL, fg=FG,
                 insertbackground=FG, font=MONO_S, relief="flat").grid(row=0, column=1, padx=(6, 18))
        tk.Label(top, text="pose feed", bg=BG, fg=DIM, font=UI).grid(row=0, column=2, sticky="w")
        tk.Entry(top, textvariable=self.pose_url, width=30, bg=PANEL, fg=FG,
                 insertbackground=FG, font=MONO_S, relief="flat").grid(row=0, column=3, padx=(6, 10))
        self.light = tk.Label(top, text="●", bg=BG, fg=WARN, font=("Segoe UI", 14))
        self.light.grid(row=0, column=4)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", padx=10, pady=4)

        left = tk.Frame(body, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(left, text="ACTUAL POSE  (camera FreeD, ~60 Hz)", bg=PANEL, fg=DIM,
                 font=UI).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2))
        self.l_yaw = self._pose_row(left, "YAW", 1)
        self.l_pitch = self._pose_row(left, "PITCH", 2)
        self.l_roll_cam = self._pose_row(left, "ROLL camera (FreeD)", 3)
        self.l_roll = self._pose_row(left, "ROLL lens (Q/E)", 4)
        self.l_roll.configure(fg=ACCENT)     # the row Q/E actually moves

        sep = tk.Frame(left, bg=PANEL2, height=1)
        sep.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        self.l_xyz = self._small_row(left, 6)
        self.l_zoom = self._small_row(left, 7)
        self.l_age = self._small_row(left, 8)

        right = tk.Frame(body, bg=PANEL)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right, text="DRIVE  (hold) · speed", bg=PANEL, fg=DIM,
                 font=UI).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 2))

        self._hold_btn(right, "▲", -1, 0, 1, 1)
        self._hold_btn(right, "◀", 0, -1, 2, 0)
        self._hold_btn(right, "▶", 0, +1, 2, 2)
        self._hold_btn(right, "▼", +1, 0, 3, 1)
        stop = tk.Button(right, text="■", command=self._stop,
                         bg=PANEL2, fg=BAD, activebackground=BAD, activeforeground=BG,
                         font=("Segoe UI", 16, "bold"), width=3, relief="flat")
        stop.grid(row=2, column=1, padx=3, pady=3, ipady=6)

        self.speed_scale = tk.Scale(right, from_=5, to=80, orient="horizontal",
                                    variable=self.speed, bg=PANEL, fg=FG,
                                    troughcolor=PANEL2, highlightthickness=0,
                                    activebackground=ACCENT, length=190, showvalue=True)
        self.speed_scale.grid(row=4, column=0, columnspan=3, pady=(2, 6))
        # After using the slider, hand the keyboard back to the window: while the
        # scale holds focus the arrow keys adjust the scale, not the gimbal.
        self.speed_scale.bind("<ButtonRelease-1>", lambda e: self.root.focus_set())
        # ...and route keys through our handler even while the scale has focus
        # (the returned break stops the class binding, so when the window itself
        # has focus -- the common case -- only the root handler fires).
        self.speed_scale.bind("<KeyPress>", self._key_down_break)
        self.speed_scale.bind("<KeyRelease>", self._key_up_break)

        zoom = tk.Frame(right, bg=PANEL)
        zoom.grid(row=5, column=0, columnspan=3, pady=2)
        for ratio in (1, 2, 3, 4):
            tk.Button(zoom, text="%d×" % ratio, command=lambda r=ratio: self._zoom_set(float(r)),
                      bg=PANEL2, fg=FG, activebackground=ACCENT, font=MONO_S,
                      width=3, relief="flat").pack(side="left", padx=2)
        tk.Button(zoom, text="−", command=lambda: self._zoom_step(-ZOOM_STEP),
                  bg=PANEL2, fg=FG, activebackground=ACCENT, font=MONO_S,
                  width=2, relief="flat").pack(side="left", padx=(10, 2))
        tk.Button(zoom, text="+", command=lambda: self._zoom_step(+ZOOM_STEP),
                  bg=PANEL2, fg=FG, activebackground=ACCENT, font=MONO_S,
                  width=2, relief="flat").pack(side="left", padx=2)

        home = tk.Button(right, text="⌂  HOME  (recenter)", command=self._home,
                         bg=PANEL2, fg=WARN, activebackground=WARN, activeforeground=BG,
                         font=("Segoe UI", 11, "bold"), relief="flat")
        home.grid(row=6, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 2), ipady=8)

        track = tk.Button(right, text="AI tracking: off", command=self._toggle_tracking,
                          bg=PANEL2, fg=DIM, activebackground=PANEL2,
                          font=UI, relief="flat")
        track.grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=(2, 8))
        self.track_btn = track

        self.status = tk.Label(root, text="ready", bg=BG, fg=DIM, font=UI,
                               anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(root, text="keys: WASD/arrows drive · space stop · H home · Z/X zoom · Q/E lens roll 1° (±10)",
                 bg=BG, fg=DIM, font=UI, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        root.bind("<KeyPress>", self._key_down)
        root.bind("<KeyRelease>", self._key_up)
        root.bind("<FocusOut>", lambda e: self._stop())
        root.protocol("WM_DELETE_WINDOW", self._quit)

    def _pose_row(self, parent, label, row):
        tk.Label(parent, text=label, bg=PANEL, fg=DIM, font=UI).grid(
            row=row, column=0, sticky="w", padx=(12, 4))
        value = tk.Label(parent, text="—", bg=PANEL, fg=FG, font=MONO, width=8, anchor="e")
        value.grid(row=row, column=1, sticky="e", padx=(0, 12))
        return value

    def _small_row(self, parent, row):
        value = tk.Label(parent, text="—", bg=PANEL, fg=DIM, font=MONO_S, anchor="w")
        value.grid(row=row, column=0, columnspan=2, sticky="w", padx=12)
        return value

    def _hold_btn(self, parent, text, dpitch, dyaw, row, col):
        button = tk.Button(parent, text=text, bg=PANEL2, fg=FG,
                           activebackground=ACCENT, font=("Segoe UI", 16, "bold"),
                           width=3, relief="flat")
        button.grid(row=row, column=col, padx=3, pady=3, ipady=6)
        button.bind("<ButtonPress-1>", lambda e: self._set_dir(dpitch, dyaw))
        button.bind("<ButtonRelease-1>", lambda e: self._clear_dir(dpitch, dyaw))

    # -------------------------------------------------------------- plumbing

    def _cam(self):
        return "http://%s/camera/sdk" % self.camera.get().strip()

    def _fire(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _set_status(self, text):
        self._status_q.put(text)

    def _set_dir(self, dpitch, dyaw):
        with self._lock:
            if dpitch:
                self.vector["pitch"] = dpitch
            if dyaw:
                self.vector["yaw"] = dyaw

    def _clear_dir(self, dpitch, dyaw):
        with self._lock:
            if dpitch and self.vector["pitch"] == dpitch:
                self.vector["pitch"] = 0
            if dyaw and self.vector["yaw"] == dyaw:
                self.vector["yaw"] = 0

    def _key_down(self, event):
        key = event.keysym.lower()
        if key in ("up", "w"):
            self._set_dir(-1, 0)
        elif key in ("down", "s"):
            self._set_dir(+1, 0)
        elif key in ("left", "a"):
            self._set_dir(0, -1)
        elif key in ("right", "d"):
            self._set_dir(0, +1)
        elif key == "space":
            self._stop()
        elif key == "h":
            self._home()
        elif key == "z":
            self._zoom_step(+ZOOM_STEP)
        elif key == "x":
            self._zoom_step(-ZOOM_STEP)
        elif key == "q":
            self._roll_step(-ROLL_STEP)
        elif key == "e":
            self._roll_step(+ROLL_STEP)

    def _key_up(self, event):
        key = event.keysym.lower()
        if key in ("up", "w"):
            self._clear_dir(-1, 0)
        elif key in ("down", "s"):
            self._clear_dir(+1, 0)
        elif key in ("left", "a"):
            self._clear_dir(0, -1)
        elif key in ("right", "d"):
            self._clear_dir(0, +1)

    def _key_down_break(self, event):
        self._key_down(event)
        return "break"

    def _key_up_break(self, event):
        self._key_up(event)
        return "break"

    # ------------------------------------------------------------- commands

    def _gimbal(self, pitch, yaw, stop=False):
        status, body = rest(self._cam() + "/ptz/gimbalcontrol", "POST",
                            {"stop": stop, "pitch": pitch, "yaw": yaw, "roll": 0})
        if stop:
            self._set_status("stop -> %s" % status)
        elif status != 200:
            self._set_status("gimbal ERR: %s" % (body,))

    def _stop(self):
        with self._lock:
            self.vector["pitch"] = self.vector["yaw"] = 0
        self._fire(self._gimbal, 0, 0, True)

    def _home(self):
        def run():
            status, body = rest(self._cam() + "/ptz/reset", "POST", {})
            self._set_status("HOME /ptz/reset -> %s %s" % (status, body or ""))
        self._fire(run)

    def _zoom_set(self, ratio):
        ratio = max(ZOOM_MIN, min(ZOOM_MAX, round(ratio, 1)))
        self.zoom_target = ratio

        def run():
            status, body = rest(self._cam() + "/ptz/zoom", "PUT",
                                {"ratio": ratio, "speed": ZOOM_SPEED})
            self._set_status("zoom -> %.1f× : %s %s" % (ratio, status, body or ""))
        self._fire(run)

    def _zoom_step(self, delta):
        self._zoom_set(self.zoom_target + delta)

    def _roll_step(self, delta):
        """Lens roll bias, -10..+10 degrees (the gimbal itself has no roll axis).

        1-degree steps: 20 steps across the range, centre (0) to each extreme.
        Holding Q or E repeats -- the OS auto-repeat keeps feeding steps -- so
        sweeping end to end is a key-hold rather than twenty presses.
        """
        self.roll_bias = max(ROLL_MIN, min(ROLL_MAX, round(self.roll_bias + delta, 1)))
        self._roll_grace = time.time() + 2.0

        def run():
            status, body = rest(self._cam() + "/ptz/rollbias", "PUT", {"angle": self.roll_bias})
            self._set_status("roll bias -> %+.0f° : %s %s" % (self.roll_bias, status, body or ""))
        self._fire(run)

    def _toggle_tracking(self):
        # If the rig is on (or waiting for a subject), the next click turns it
        # off; otherwise it arms single-person tracking.
        want_on = self._track_state not in ("on", "waiting")
        self.track_request = want_on
        self._track_request_time = time.time()

        def run():
            # The camera's real enum (from its own web app): none,
            # humanTrackingSingleMode, humanTrackingGroupMode, animalTrackingNormal,
            # animalTrackingCloseUp, objectTracking, ...  "normalTrack" is NOT a mode.
            # NOTE: with nobody in view the camera answers 200 but keeps reporting
            # "none" -- the read-back means "tracking engaged or not", so the UI
            # below distinguishes armed-and-waiting from actually on.
            mode = "humanTrackingSingleMode" if want_on else "none"
            status, body = rest(self._cam() + "/ai/workmode", "PUT", {"mode": mode})
            self._set_status("AI tracking %s -> %s %s" % ("ON" if want_on else "off", status, body or ""))
        self._fire(run)

    def _quit(self):
        self._fire(self._gimbal, 0, 0, True)
        self.root.after(120, self.root.destroy)

    # -------------------------------------------------------------- threads

    def _pose_loop(self):
        while True:
            try:
                status, body = rest(self.pose_url.get().strip(), "GET", timeout=1.0)
                if status == 200 and isinstance(body, dict) and body.get("ok"):
                    self.pose = body
                    self.pose_error = None
                    self.pose_wall = time.time()
                elif status == 200:
                    self.pose_error = (body or {}).get("error", "no samples")
                else:
                    self.pose_error = body
            except Exception as exc:
                self.pose_error = str(exc)
            time.sleep(1.0 / POSE_HZ)

    def _drive_loop(self):
        last_send = 0.0
        was_driving = False
        while True:
            with self._lock:
                dpitch, dyaw = self.vector["pitch"], self.vector["yaw"]
            driving = bool(dpitch or dyaw)
            now = time.time()
            if driving:
                if now - last_send >= 1.0 / HOLD_HZ:
                    last_send = now
                    speed = int(self.speed.get())
                    self._fire(self._gimbal, dpitch * speed, dyaw * speed)
                was_driving = True
            elif was_driving:
                was_driving = False
                self._fire(self._gimbal, 0, 0, True)
            time.sleep(0.04)

    def _mode_loop(self):
        """Read the camera's real AI workmode and roll bias so the UI shows truth."""
        while True:
            status, body = rest(self._cam() + "/ai/workmode", "GET", timeout=1.5)
            if status == 200 and isinstance(body, dict):
                self.mode_remote = str(body.get("mode", ""))

            status, body = rest(self._cam() + "/ptz/rollbias", "GET", timeout=1.5)
            if (status == 200 and isinstance(body, dict) and "angle" in body
                    and time.time() > self._roll_grace):
                try:
                    self.roll_bias = max(ROLL_MIN, min(ROLL_MAX, float(body["angle"])))
                except (TypeError, ValueError):
                    pass
            time.sleep(2.0)

    # ------------------------------------------------------------- repaint

    def _refresh(self):
        # status line
        try:
            while True:
                self.status.configure(text=self._status_q.get_nowait())
        except queue.Empty:
            pass

        # pose panel
        pose = self.pose
        have = pose is not None
        if have:
            self.l_yaw.configure(text="%+.2f°" % pose.get("yaw", 0.0))
            self.l_pitch.configure(text="%+.2f°" % pose.get("pitch", 0.0))
            self.l_roll_cam.configure(text="%+.2f°" % pose.get("roll", 0.0))
            self.l_xyz.configure(text="X %+.1f · Y %+.1f · Z %+.1f cm"
                                 % (pose.get("x", 0.0), pose.get("y", 0.0), pose.get("z", 0.0)))
            focal = pose.get("focal", 0)
            estimate = max(0.0, (focal - 12) / 1000.0)
            self.l_zoom.configure(text="FOCAL %d  (≈%.2f×)   FOCUS %d"
                                  % (focal, estimate, pose.get("focus", 0)))

            count = pose.get("packets", 0)
            now = self.pose_wall
            if self._last_count is not None and now > self._last_count_time:
                self.rate = (count - self._last_count) / (now - self._last_count_time)
            self._last_count, self._last_count_time = count, now
            self.l_age.configure(text="age %.2f s · %d pkts · %.0f/s"
                                 % (pose.get("age_s", 0.0), count, self.rate))
        else:
            for label in (self.l_yaw, self.l_pitch, self.l_roll_cam,
                          self.l_xyz, self.l_zoom, self.l_age):
                label.configure(text="—")

        # Two rolls, one row each. "ROLL camera" above is the head's own roll
        # from the FreeD stream (this head has no roll axis -- it stays ~0.00
        # by design). "ROLL lens" here is the lens roll bias Q/E drives; it is
        # known even while the pose feed is down, so it always shows a number.
        self.l_roll.configure(text="%+.0f°" % self.roll_bias)

        # tracking button mirrors the camera's real state, in three flavours:
        # off / waiting for a person / on. The camera reports "none" while it has
        # nobody to track, so a request that answers 200 can still mean
        # "armed, waiting" -- say exactly that instead of lying either way.
        remote_on = self.mode_remote not in (None, "", "none")
        waiting = (self.track_request is True and not remote_on
                   and time.time() - self._track_request_time > 6.0)
        state = ("on" if (remote_on or (self.track_request is True and not waiting))
                 else ("waiting" if waiting else "off"))
        if state != self._track_state:
            self._track_state = state
            if state == "on":
                self.track_btn.configure(text="AI tracking: on", fg=GOOD)
                if remote_on:
                    self._set_status("AI tracking engaged (camera reports %s)" % self.mode_remote)
            elif state == "waiting":
                self.track_btn.configure(text="AI tracking: waiting for a person", fg=WARN)
                self._set_status("no subject in view -- step in front of the camera; "
                                 "it starts following as soon as it sees someone")
            else:
                self.track_btn.configure(text="AI tracking: off", fg=DIM)

        # stream light
        if have and pose.get("age_s", 99) < 0.5:
            self.light.configure(fg=GOOD)
        elif have and pose.get("age_s", 99) < 3.0:
            self.light.configure(fg=WARN)
        else:
            self.light.configure(fg=BAD)
            if not have and self.pose_error:
                self.status.configure(text="no pose feed (%s) — is tail2_freed_server.py running?"
                                      % self.pose_error)

        self.root.after(80, self._refresh)


def main():
    root = tk.Tk()
    Console(root)
    root.mainloop()


if __name__ == "__main__":
    main()
