#!/usr/bin/env python3
"""
Delivery truck detector — Pi-only, two-stage.

Stage 1: Roboflow hosted YOLO (free tier) detects truck/bus class
Stage 2: Gemini Flash confirms UPS/FedEx branding (only when stage 1 triggers)

Cost: ~$0.30/month (Roboflow free, Gemini only on detections)
"""

import os
import sys
import time
import base64
import tempfile
import subprocess
import logging

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# Camera
CAMERA_IP = os.environ.get("CAMERA_IP", "10.0.0.88")
CAMERA_USER = os.environ.get("CAMERA_USER", "orintapo")
CAMERA_PASS = os.environ.get("CAMERA_PASS", "nvidia")
STREAM = os.environ.get("STREAM", "stream1")

# Roboflow (free tier — stage 1)
ROBOFLOW_API_KEY = os.environ.get("ROBOFLOW_API_KEY", "")
ROBOFLOW_MODEL = os.environ.get("ROBOFLOW_MODEL", "coco/3")
ROBOFLOW_CONFIDENCE = int(os.environ.get("ROBOFLOW_CONFIDENCE", "20"))
VEHICLE_CLASSES = {"truck", "bus"}  # delivery trucks register as either

# Gemini (stage 2 — only called on detections)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3-flash-preview")

# Pushover
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN = os.environ.get("PUSHOVER_APP_TOKEN", "")

# Timing
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "30"))
COOLDOWN = int(os.environ.get("COOLDOWN_SECONDS", "600"))
START_HOUR = int(os.environ.get("START_HOUR", "8"))
END_HOUR = int(os.environ.get("END_HOUR", "20"))

last_notification_time = 0
clear_streak = 0  # consecutive checks with no vehicle
CLEAR_STREAK_RESET = 3  # reset cooldown after this many clear checks


def grab_frame(rtsp_url, output_path):
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-i", rtsp_url, "-frames:v", "1", "-update", "1", "-q:v", "5",
         output_path],
        capture_output=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg: " + result.stderr.decode().strip())


def stage1_roboflow(image_path):
    """Roboflow YOLO: detect truck/bus. Returns (detected, best_conf, class_name)."""
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    resp = requests.post(
        "https://detect.roboflow.com/" + ROBOFLOW_MODEL,
        params={"api_key": ROBOFLOW_API_KEY, "confidence": ROBOFLOW_CONFIDENCE},
        data=img_b64,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()

    best_conf = 0
    best_class = ""
    for p in resp.json().get("predictions", []):
        if p["class"] in VEHICLE_CLASSES and p["confidence"] > best_conf:
            best_conf = p["confidence"]
            best_class = p["class"]

    return best_conf > 0, best_conf, best_class


def stage2_gemini(image_path):
    """Gemini Flash: confirm UPS/FedEx. Returns True/False."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": "Bearer " + OPENROUTER_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "max_tokens": 20,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {
                                "url": "data:image/jpeg;base64," + b64}},
                            {"type": "text", "text": (
                                "Is there a UPS or FedEx delivery truck clearly "
                                "visible in this image? ONLY say YES for vehicles "
                                "with UPS or FedEx branding. Say NO for all other "
                                "vehicles including Amazon, USPS, unmarked vans, "
                                "and anything else. Reply YES or NO only."
                            )},
                        ],
                    }],
                },
                timeout=30,
            )
            resp.raise_for_status()
            answer = (resp.json()["choices"][0]["message"].get("content") or "").strip().upper()
            is_delivery = "YES" in answer
            log.info("Gemini: %s -> %s", answer, "DELIVERY" if is_delivery else "NOT DELIVERY")
            return is_delivery
        except Exception as e:
            if attempt < 2:
                log.warning("Gemini attempt %d failed: %s", attempt + 1, e)
                time.sleep(1)
            else:
                log.error("Gemini failed after 3 attempts — skipping notification")
                return False


def send_notification(image_path):
    global last_notification_time

    now = time.time()
    if now - last_notification_time < COOLDOWN:
        log.info("Cooldown active (%ds left)", int(COOLDOWN - (now - last_notification_time)))
        return False

    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        log.warning("Pushover not configured")
        return False

    with open(image_path, "rb") as f:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_APP_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "message": "UPS or FedEx truck spotted! Go grab your package!",
                "title": "Delivery Truck Alert",
                "priority": 1,
                "sound": "siren",
            },
            files={"attachment": ("truck.jpg", f, "image/jpeg")},
            timeout=10,
        )
    if resp.status_code != 200:
        log.error("Pushover returned %d: %s", resp.status_code, resp.text[:200])
        return False
    last_notification_time = now
    log.info("Notification sent!")
    return True


def in_active_hours():
    return START_HOUR <= time.localtime().tm_hour < END_HOUR


def main():
    if not ROBOFLOW_API_KEY:
        print("ERROR: ROBOFLOW_API_KEY not set")
        sys.exit(1)

    rtsp_url = "rtsp://{}:{}@{}:554/{}".format(CAMERA_USER, CAMERA_PASS, CAMERA_IP, STREAM)
    tmp_path = os.path.join(tempfile.gettempdir(), "truck_frame.jpg")

    log.info("Waiting for camera at %s...", CAMERA_IP)
    for attempt in range(30):
        try:
            grab_frame(rtsp_url, tmp_path)
            log.info("Camera OK")
            break
        except Exception as e:
            if attempt < 29:
                log.warning("Camera not ready (attempt %d/30): %s", attempt + 1, e)
                time.sleep(10)
            else:
                log.error("Camera failed after 30 attempts — exiting")
                sys.exit(1)

    log.info("Detector starting (every %ds, active %d:00-%d:00)", INTERVAL, START_HOUR, END_HOUR)
    log.info("Stage 1: Roboflow %s | Stage 2: Gemini Flash", ROBOFLOW_MODEL)

    global clear_streak, last_notification_time

    frame_count = 0
    while True:
        try:
            if not in_active_hours():
                if frame_count > 0:
                    log.info("Outside active hours — sleeping")
                    frame_count = 0
                time.sleep(60)
                continue

            grab_frame(rtsp_url, tmp_path)

            # Stage 1: Roboflow YOLO (free)
            detected, conf, cls = stage1_roboflow(tmp_path)
            frame_count += 1

            if not detected:
                clear_streak += 1
                if clear_streak == CLEAR_STREAK_RESET and last_notification_time > 0:
                    log.info("Scene clear for %d checks — cooldown reset", CLEAR_STREAK_RESET)
                    last_notification_time = 0
                if frame_count % 10 == 0:
                    log.info("Monitoring... (%d checks, all clear)", frame_count)
                time.sleep(INTERVAL)
                continue

            clear_streak = 0  # vehicle present, reset clear streak

            # Skip stage 2 if cooldown is active (same truck still there)
            now = time.time()
            if now - last_notification_time < COOLDOWN:
                time.sleep(INTERVAL)
                continue

            log.info("Stage 1: %s detected (conf=%.2f) — checking with Gemini...", cls, conf)

            # Stage 2: Gemini confirmation (costs ~$0.0006)
            if stage2_gemini(tmp_path):
                log.info(">>> DELIVERY TRUCK CONFIRMED!")
                send_notification(tmp_path)
            else:
                log.info("Gemini says not a delivery truck — skipping")

        except KeyboardInterrupt:
            break
        except Exception as e:
            if frame_count % 10 == 0 or frame_count < 5:
                log.error("Error: %s", e)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
