#!/usr/bin/env python3
"""
Snapshot forwarder for Raspberry Pi Zero 2 W.

Grabs JPEG snapshots from a local IP camera and POSTs them to the
Jetson Orin AGX over a Tailscale tunnel.

The camera is a TP-Link Tapo C200/C210/C310 (or similar) on the local network.
Supports both HTTP snapshot and RTSP (via ffmpeg) capture methods.
"""

import os
import time
import subprocess
import logging

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# === CONFIGURATION ===
# Set these for your specific setup, or use environment variables.

# Camera snapshot URL — try HTTP first, fall back to RTSP
# Tapo cameras: RTSP is the reliable option
# HTTP snapshot (if supported): "http://<camera-ip>/snapshot.jpg"
CAMERA_SNAPSHOT_URL = os.environ.get("CAMERA_SNAPSHOT_URL", "")

# RTSP URL — used if CAMERA_SNAPSHOT_URL is empty
# Tapo default: rtsp://<user>:<pass>@<camera-ip>:554/stream1
CAMERA_RTSP_URL = os.environ.get(
    "CAMERA_RTSP_URL",
    "rtsp://USER:PASS@CAMERA_IP:554/stream1",  # <-- EDIT THIS
)

# Jetson's Tailscale IP + port
JETSON_URL = os.environ.get(
    "JETSON_URL",
    "http://100.118.29.32:5555/upload",  # <-- Ben's Orin Tailscale IP
)

INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "3"))
SNAPSHOT_PATH = "/tmp/frame.jpg"
# =====================


def grab_http():
    """Grab a JPEG via HTTP snapshot endpoint."""
    resp = requests.get(CAMERA_SNAPSHOT_URL, timeout=5)
    resp.raise_for_status()
    with open(SNAPSHOT_PATH, "wb") as f:
        f.write(resp.content)


def grab_rtsp():
    """Grab a single JPEG frame via ffmpeg from RTSP."""
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", CAMERA_RTSP_URL,
            "-frames:v", "1",
            "-q:v", "5",
            SNAPSHOT_PATH,
        ],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()[-200:]}")


def send_to_jetson():
    """POST the JPEG to the Jetson's HTTP endpoint."""
    with open(SNAPSHOT_PATH, "rb") as f:
        resp = requests.post(
            JETSON_URL,
            files={"image": ("frame.jpg", f, "image/jpeg")},
            timeout=10,
        )
    return resp.status_code, resp.json()


def main():
    grab = grab_http if CAMERA_SNAPSHOT_URL else grab_rtsp
    method = "HTTP" if CAMERA_SNAPSHOT_URL else "RTSP"
    log.info("Starting forwarder (%s, interval=%ds) -> %s",
             method, INTERVAL_SECONDS, JETSON_URL)

    while True:
        try:
            grab()
            status, body = send_to_jetson()
            if status != 200:
                log.warning("Jetson returned %d: %s", status, body)
            elif body.get("status") != "no_truck":
                log.info("Detection result: %s", body)
        except Exception as e:
            log.error("Error: %s", e)

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
