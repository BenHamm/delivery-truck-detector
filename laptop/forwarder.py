#!/usr/bin/env python3
"""
Snapshot forwarder — laptop edition.

Grabs JPEG snapshots from a Tapo C320WS on the local network and
POSTs them to the Jetson Orin AGX over Tailscale.

Works on Windows, Mac, or Linux. Requires: Python 3, ffmpeg, requests.

Usage:
    python forwarder.py
    python forwarder.py --camera-ip 192.168.1.50 --camera-user admin --camera-pass mypass
    python forwarder.py --interval 5
"""

import os
import sys
import time
import tempfile
import argparse
import subprocess
import logging

try:
    import requests
except ImportError:
    print("Missing 'requests' library. Run: pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# Defaults — override via args or env vars
CAMERA_IP = os.environ.get("CAMERA_IP", "")
CAMERA_USER = os.environ.get("CAMERA_USER", "")
CAMERA_PASS = os.environ.get("CAMERA_PASS", "")
JETSON_URL = os.environ.get("JETSON_URL", "http://100.118.29.32:5555/upload")
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "3"))


def find_camera_ip():
    """Try to discover the Tapo camera on the local network."""
    # Common Tapo default IPs — not reliable, but worth a shot
    import socket
    candidates = [CAMERA_IP] if CAMERA_IP else []

    for ip in candidates:
        try:
            sock = socket.create_connection((ip, 554), timeout=2)
            sock.close()
            return ip
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue
    return None


def grab_frame(rtsp_url, output_path):
    """Grab a single JPEG frame via ffmpeg."""
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "5",
            output_path,
        ],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg: {result.stderr.decode().strip()}")


def send_to_jetson(image_path, jetson_url):
    """POST the JPEG to the Jetson."""
    with open(image_path, "rb") as f:
        resp = requests.post(
            jetson_url,
            files={"image": ("frame.jpg", f, "image/jpeg")},
            timeout=15,
        )
    return resp.status_code, resp.json()


def test_rtsp(rtsp_url):
    """Verify RTSP connection works."""
    tmp = os.path.join(tempfile.gettempdir(), "test_frame.jpg")
    try:
        grab_frame(rtsp_url, tmp)
        size = os.path.getsize(tmp)
        log.info("Camera test OK — got %d KB frame", size // 1024)
        return True
    except Exception as e:
        log.error("Camera test FAILED: %s", e)
        return False


def test_jetson(jetson_url):
    """Verify Jetson is reachable."""
    try:
        resp = requests.get(jetson_url.replace("/upload", "/health"), timeout=5)
        if resp.status_code == 200:
            log.info("Jetson connection OK")
            return True
    except Exception as e:
        log.error("Jetson test FAILED: %s", e)
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Forward camera snapshots to the Jetson for truck detection"
    )
    parser.add_argument("--camera-ip", default=CAMERA_IP, required=not CAMERA_IP,
                        help="Tapo camera IP on local network")
    parser.add_argument("--camera-user", default=CAMERA_USER, required=not CAMERA_USER,
                        help="Camera RTSP username (set in Tapo app)")
    parser.add_argument("--camera-pass", default=CAMERA_PASS, required=not CAMERA_PASS,
                        help="Camera RTSP password")
    parser.add_argument("--jetson", default=JETSON_URL,
                        help="Jetson server URL (default: %(default)s)")
    parser.add_argument("--interval", type=int, default=INTERVAL,
                        help="Seconds between frames (default: %(default)s)")
    parser.add_argument("--stream", default="stream1",
                        help="RTSP stream path (stream1=high res, stream2=low res)")
    args = parser.parse_args()

    rtsp_url = f"rtsp://{args.camera_user}:{args.camera_pass}@{args.camera_ip}:554/{args.stream}"
    tmp_path = os.path.join(tempfile.gettempdir(), "truck_frame.jpg")

    print()
    print("=== Delivery Truck Detector — Laptop Relay ===")
    print(f"  Camera:  {args.camera_ip} ({args.stream})")
    print(f"  Jetson:  {args.jetson}")
    print(f"  Interval: {args.interval}s")
    print()

    # Pre-flight checks
    log.info("Testing camera connection...")
    if not test_rtsp(rtsp_url):
        print("\nCannot reach camera. Check:")
        print(f"  - Is the camera on and connected to Wi-Fi?")
        print(f"  - Is {args.camera_ip} the right IP? (Check in Tapo app)")
        print(f"  - Are the RTSP credentials correct? (Set in Tapo app > Camera Account)")
        sys.exit(1)

    log.info("Testing Jetson connection...")
    if not test_jetson(args.jetson):
        print("\nCannot reach Jetson. Check:")
        print("  - Is Tailscale connected? (Check Tailscale icon in system tray)")
        print(f"  - Is the Jetson running the server? (http health check failed)")
        sys.exit(1)

    print("\nAll checks passed. Starting relay... (Ctrl+C to stop)\n")

    frame_count = 0
    error_count = 0

    while True:
        try:
            grab_frame(rtsp_url, tmp_path)
            status, body = send_to_jetson(tmp_path, args.jetson)
            frame_count += 1
            error_count = 0

            if body.get("status") == "no_vehicle":
                if frame_count % 20 == 0:  # Log every 20th clear frame
                    log.info("Monitoring... (%d frames, all clear)", frame_count)
            else:
                log.info(">>> DETECTION: %s", body)

        except KeyboardInterrupt:
            print(f"\nStopped after {frame_count} frames.")
            break
        except Exception as e:
            error_count += 1
            log.error("Error: %s", e)
            if error_count >= 5:
                log.error("Too many consecutive errors — check camera and network")
                error_count = 0
                time.sleep(10)
                continue

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
