#!/usr/bin/env python3
"""
Delivery truck detector — Pi-only, single-stage.

Stage 1: Gemini Flash directly confirms UPS/FedEx branding on every frame.

Cost: ~$1.50/month (Gemini Flash via OpenRouter on every poll)
"""

import os
import sys
import time
import glob
import shutil
import base64
import tempfile
import subprocess
import logging
import signal

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# Camera
CAMERA_IP = os.environ.get("CAMERA_IP", "10.0.0.88")
CAMERA_USER = os.environ.get("CAMERA_USER", "orintapo")
CAMERA_PASS = os.environ.get("CAMERA_PASS", "nvidia")
STREAM = os.environ.get("STREAM", "stream1")

# Gemini Flash (single stage — called on every frame)
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

# Rolling image log
LOG_DIR = os.environ.get("LOG_DIR", "/home/pi/detections")
LOG_RETENTION_HOURS = int(os.environ.get("LOG_RETENTION_HOURS", "36"))
NO_SAMPLE_INTERVAL = int(os.environ.get("NO_SAMPLE_INTERVAL", "1800"))  # save 1 NO frame every 30min

# Healthchecks.io dead-man's switch
HC_PING_URL = os.environ.get("HC_PING_URL", "https://hc-ping.com/1d4cb30e-1d3e-4425-b6d9-f1f93590ca4c")
HC_INTERVAL = 1800  # ping every 30 minutes

last_notification_time = 0
clear_streak = 0
CLEAR_STREAK_RESET = 3


def grab_frame(rtsp_url, output_path):
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-i", rtsp_url, "-frames:v", "1", "-update", "1", "-q:v", "5",
         output_path],
        capture_output=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg: " + result.stderr.decode().strip())


def call_gemini(image_path, alarm_s=25):
    """Gemini Flash: confirm UPS/FedEx. Returns True/False.

    Hard SIGALRM backstop prevents a stalled HTTP call from blocking
    long enough to trigger the systemd watchdog (1 min). alarm_s=25 for
    primary polls, alarm_s=15 for confirm polls (where worst-case must
    stack under 60s: 25 + 5 sleep + 15 = 45s).
    """
    def _alarm_handler(signum, frame):
        raise TimeoutError("Gemini hard timeout")

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(alarm_s)
    try:
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
                    timeout=(10, 20),  # (connect, read) — separate timeouts
                )
                resp.raise_for_status()
                answer = (resp.json()["choices"][0]["message"].get("content") or "").strip().upper()
                is_delivery = "YES" in answer
                log.info("Gemini: %s -> %s", answer, "DELIVERY" if is_delivery else "NOT DELIVERY")
                ping_healthcheck()  # successful Gemini call = pipeline healthy
                return is_delivery
            except TimeoutError:
                raise  # let SIGALRM bubble up to outer handler
            except Exception as e:
                if attempt < 2:
                    log.warning("Gemini attempt %d failed: %s", attempt + 1, e)
                    time.sleep(1)
                else:
                    log.error("Gemini failed after 3 attempts — skipping notification")
                    return False
    except TimeoutError:
        log.error("Gemini hard timeout (%ds) — skipping frame", alarm_s)
        return False
    finally:
        signal.alarm(0)  # always cancel the alarm


_last_no_save = 0

def save_detection(image_path, gemini_verdict, suffix=""):
    """Save YES frames always; NO frames only every NO_SAMPLE_INTERVAL seconds
    to reduce SD card wear. Suffixed frames (tentative/confirm) always save —
    they're tied to a detection event, not the rolling background sample.
    Returns True if saved, False if skipped."""
    global _last_no_save
    now = time.time()
    if not suffix and not gemini_verdict and (now - _last_no_save) < NO_SAMPLE_INTERVAL:
        return False  # skip — recent NO already sampled

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    verdict = "YES" if gemini_verdict else "NO"
    shutil.copy(image_path, os.path.join(LOG_DIR, f"{verdict}_{ts}{suffix}.jpg"))
    if not suffix and not gemini_verdict:
        _last_no_save = now
    return True


_last_hc_ping = 0

def ping_healthcheck(force=False):
    """Fire-and-forget ping to healthchecks.io. Rate-limited to HC_INTERVAL
    unless force=True. Failure is non-fatal."""
    global _last_hc_ping
    now = time.time()
    if not force and (now - _last_hc_ping) < HC_INTERVAL:
        return
    try:
        requests.get(HC_PING_URL, timeout=5)
        _last_hc_ping = now
        log.info("Healthcheck ping sent")
    except Exception as e:
        log.warning("Healthcheck ping failed: %s", e)


def cleanup_old_detections():
    """Remove detection files older than LOG_RETENTION_HOURS."""
    cutoff = time.time() - LOG_RETENTION_HOURS * 3600
    removed = 0
    for f in glob.glob(os.path.join(LOG_DIR, "*")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("Cleaned up %d old detection files", removed)


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
    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set")
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
    log.info("Single-stage: Gemini Flash direct (every %ds)", INTERVAL)
    log.info("Detection log: %s (%dh retention)", LOG_DIR, LOG_RETENTION_HOURS)

    global clear_streak, last_notification_time

    frame_count = 0
    last_cleanup = 0
    ping_healthcheck(force=True)  # always ping on startup so we know Pi came back up
    while True:
        try:
            if not in_active_hours():
                if frame_count > 0:
                    log.info("Outside active hours — sleeping")
                    frame_count = 0
                ping_healthcheck()  # overnight keepalive (rate-limited to HC_INTERVAL)
                time.sleep(60)
                continue

            grab_frame(rtsp_url, tmp_path)

            # Single-stage: Gemini Flash directly
            frame_count += 1
            now = time.time()

            # Skip Gemini if cooldown active (same truck still there)
            if now - last_notification_time < COOLDOWN:
                time.sleep(INTERVAL)
                continue

            is_delivery = call_gemini(tmp_path)

            if is_delivery:
                # Tentative — could be a truck driving past on the cross-street.
                # Wait 5s and recheck: a real delivery is still parked, a
                # drive-by is long gone.
                save_detection(tmp_path, True, suffix="_tentative")
                log.info("Tentative YES — confirming in 5s...")
                time.sleep(5)
                try:
                    grab_frame(rtsp_url, tmp_path)
                    confirmed = call_gemini(tmp_path, alarm_s=15)
                    save_detection(tmp_path, confirmed, suffix="_confirm")
                    if confirmed:
                        clear_streak = 0
                        log.info(">>> DELIVERY TRUCK CONFIRMED (2/2)!")
                        send_notification(tmp_path)
                    else:
                        log.info("Drive-by dismissed — truck gone after 5s")
                except Exception as e:
                    log.warning("Confirm step failed: %s — skipping notification", e)
            else:
                save_detection(tmp_path, False)
                clear_streak += 1
                if clear_streak == CLEAR_STREAK_RESET and last_notification_time > 0:
                    log.info("Scene clear for %d checks — cooldown reset", CLEAR_STREAK_RESET)
                    last_notification_time = 0
                if frame_count % 10 == 0:
                    log.info("Monitoring... (%d checks, all clear)", frame_count)

            # Cleanup old files once per hour
            if now - last_cleanup > 3600:
                cleanup_old_detections()
                last_cleanup = now

        except KeyboardInterrupt:
            break
        except Exception as e:
            if frame_count % 10 == 0 or frame_count < 5:
                log.error("Error: %s", e)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
