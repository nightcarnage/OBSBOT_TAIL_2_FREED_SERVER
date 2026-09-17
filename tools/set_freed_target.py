#!/usr/bin/env python3
"""Point the OBSBOT Tail 2's own FreeD sender at this machine -- once, persistently.

No OBSBOT SDK, no OBSBOT Center, no Unreal, no video. This is the setup half of a
two-part bundle: run it once, then run freed_server.py (or any D1/FreeD receiver)
forever after.

What it does, in one breath: opens a UDP session to the camera's control port (9999),
registers as a client by declaring "send your replies and pushes to <my ip>:<my port>"
(the camera sends its answers to the DECLARED port, not the source port), then sends the
SetVirtualTrack command with the target ip + port fields the camera's FreeD task dials.
The camera stores that in flash -- it survives reboots and keeps sending 29-byte D1
packets (~60 Hz, ~1.7 KB/s) with no client attached.

Usage:
  python set_freed_target.py --target-ip 192.168.15.232 --target-port 40000
  python set_freed_target.py --off                  # stop the camera sending
  python set_freed_target.py --camera 192.168.15.236 --target-port 40000

Afterwards it listens on the target port for a few seconds (if free) and reports the D1
it catches; if your receiver already holds that port the check is skipped -- watch the
receiver instead.
"""

import argparse
import socket
import struct
import time

TOKEN = bytes.fromhex("fe2a505eb863")     # any six bytes; the camera echoes the session's
                                          # first talker -- the value is not validated

# The vendor client's own opening exchange, byte-for-byte from a working session. Frame 2
# is patched at run time to declare OUR address -- its tail is the only part the camera
# reads back from it.
F2 = bytes.fromhex("aa250000140018210c0101080206fe2a505eb8630900d2e00c0100e80fa8c00cf1")
QUERIES = [
    bytes.fromhex("aa010100140011950c0d08180206fe2a505eb863"),   # uuid
    bytes.fromhex("aa010200140079110c0d08040206fe2a505eb863"),   # version
    bytes.fromhex("aa0103001400bf690c0d48190206fe2a505eb863"),   # device info
    bytes.fromhex("aa010400140004640c0dc8180206fe2a505eb863"),   # serial number
]
HEARTBEAT = bytes.fromhex("aa2509001400cc570c0181080206fe2a505eb86308001c1f0100e80fa8c00cf1")


def crc16_usb(data):
    """CRC-16/USB: poly 0xA001 (reflected), init 0xFFFF, final xor 0xFFFF (check B4C8)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def fix_crcs(msg):
    """Recompute the two CRCs a patched frame needs.

    Inner CRC (u16 at 0x16): over the message from 0x14 onward, with the field zeroed.
    Header CRC (u16 at 0x06): over bytes 0..5 + 0000 + bytes 8..(u16 at 4).
    """
    msg = bytearray(msg)

    msg[0x16:0x18] = b"\x00\x00"
    msg[0x16:0x18] = struct.pack("<H", crc16_usb(bytes(msg[0x14:])))

    msg[0x06:0x08] = b"\x00\x00"
    header_end = struct.unpack_from("<H", msg, 4)[0]
    msg[0x06:0x08] = struct.pack(
        "<H", crc16_usb(bytes(msg[0:6]) + b"\x00\x00" + bytes(msg[8:header_end])))

    return bytes(msg)


def declare_frame(my_ip, my_port):
    """Frame 2 with its 9-byte tail claiming our address.

    Tail layout:  09 00 | crc | 0c 01 00 | ip (4 bytes, little-endian) | port (u16 LE)
    Note the byte order: this tail takes the IP reversed relative to the SetVirtualTrack
    payload below -- both spellings are verified against live hardware.
    """
    frame = bytearray(F2)
    frame[0x18:0x21] = (b"\x0c\x01\x00"
                        + socket.inet_aton(my_ip)[::-1]
                        + struct.pack("<H", my_port))
    return fix_crcs(frame)


def heartbeat_frame(my_ip, my_port):
    """The ~1/s keepalive, tail re-declaring our address (same reversed-IP convention)."""
    frame = bytearray(HEARTBEAT)
    frame[0x1A:0x1E] = socket.inet_aton(my_ip)[::-1]
    frame[0x1E:0x20] = struct.pack("<H", my_port)
    return fix_crcs(frame)


def set_frame(target_ip, target_port, enable):
    """The SetVirtualTrack command (remo id 0x2B0; payload must be exactly 36 bytes).

    The daemon checks the length is 0x24 and then reads:
        ipv4 -> bytes 0x1C..0x1F   (NETWORK order)
        port -> u32 at 0x20        (little-endian)
    Anything else is silently ignored -- which is why SDK-built packets, where the ip
    lands in the port slot, never actually configure anything.
    """
    payload = bytearray(36)
    payload[0] = 1 if enable else 0
    payload[0x1C:0x20] = socket.inet_aton(target_ip)
    payload[0x20:0x24] = struct.pack("<I", target_port)

    frame = bytearray(bytes.fromhex("aa2508001400") + b"\x00\x00"
                      + bytes.fromhex("0c0202ac0206") + TOKEN)
    frame += struct.pack("<H", len(payload))
    frame += b"\x00\x00"
    frame += payload
    return fix_crcs(frame)


def guess_local_ip(camera_ip):
    """The address this machine would use to reach the camera."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((camera_ip, 9999))
        return probe.getsockname()[0]
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(
        description="Point the Tail 2's FreeD sender at this machine (stored in the camera).")
    parser.add_argument("--camera", default="192.168.15.236",
                        help="camera address (default: %(default)s)")
    parser.add_argument("--target-ip", default=None,
                        help="where the camera sends D1 (default: this machine)")
    parser.add_argument("--target-port", type=int, default=40000,
                        help="D1 UDP port (default: %(default)s)")
    parser.add_argument("--off", action="store_true",
                        help="stop the camera sending (enable = 0)")
    parser.add_argument("--verify-seconds", type=int, default=5,
                        help="how long to watch for D1 afterwards (default: %(default)s)")
    args = parser.parse_args()

    target_ip = args.target_ip or guess_local_ip(args.camera)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.8)

    # getsockname() on a 0.0.0.0 bind reports 0.0.0.0; the camera needs the real address
    # it can reach us on -- the one the OS would route to the camera with.
    my_port = sock.getsockname()[1]
    my_ip = guess_local_ip(args.camera)

    print(f"camera        : {args.camera}:9999")
    print(f"this machine  : {my_ip}  (announced to the camera as {my_ip}:{my_port})")
    print(f"D1 target     : {target_ip}:{args.target_port}  (enable = {0 if args.off else 1})")
    print()

    def drain():
        replies = 0
        while True:
            try:
                sock.recvfrom(2048)
            except socket.timeout:
                break
            replies += 1
        return replies

    def send(frame, name):
        sock.sendto(frame, (args.camera, 9999))
        time.sleep(0.18)
        print(f"  -> {name:<10} ({len(frame)}B)   replies: {drain()}")

    print("registering a control session...")
    send(declare_frame(my_ip, my_port), "hello")
    for index, query in enumerate(QUERIES):
        send(query, f"query {index + 1}")

    print("sending SetVirtualTrack...")
    frame = set_frame(target_ip, args.target_port, not args.off)
    for attempt in range(2):
        sock.sendto(frame, (args.camera, 9999))
        time.sleep(0.5)
        drain()

    print("holding the session briefly (heartbeats)...")
    beat = heartbeat_frame(my_ip, my_port)
    for _ in range(3):
        sock.sendto(beat, (args.camera, 9999))
        time.sleep(0.6)
        drain()

    sock.close()

    if args.off:
        print()
        print("done -- the camera should stop sending FreeD now.")
        return 0

    # Best-effort proof: if the target port is free, listen on it and count D1 packets.
    check = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No SO_REUSEADDR: on Windows it would let this bind "succeed" even while the
        # real receiver holds the port -- and then receive nothing. Failing here is how
        # "your receiver is running" gets detected.
        check.bind(("0.0.0.0", args.target_port))
    except OSError:
        print()
        print(f"port {args.target_port} is busy (your receiver is probably running) -- "
              f"watch it; the camera should be sending D1 now.")
        return 0

    print()
    print(f"watching {args.target_port} for {args.verify_seconds}s of D1...")
    check.settimeout(1.0)
    count = 0
    first = None
    deadline = time.time() + args.verify_seconds
    while time.time() < deadline:
        try:
            data, addr = check.recvfrom(2048)
        except socket.timeout:
            continue
        if len(data) >= 29 and data[0] == 0xD1:
            count += 1
            if first is None:
                first = (addr, data)
    check.close()

    if count:
        addr, data = first
        yaw = int.from_bytes(data[2:5], "big", signed=True) / 32768.0
        pitch = int.from_bytes(data[5:8], "big", signed=True) / 32768.0
        print(f"SUCCESS: {count} D1 packets in {args.verify_seconds}s from {addr[0]} "
              f"(first: yaw {yaw:.3f}, pitch {pitch:.3f})")
        print("The camera keeps sending this -- through reboots, with nothing else running.")
        print("Run freed_server.py to use it.")
        return 0

    print("no D1 seen yet. If the camera was mid-something, give it a few seconds and start "
          "freed_server.py; otherwise re-check the camera address.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
