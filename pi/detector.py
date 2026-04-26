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

# Pushover — two recipient tiers
# PUSHOVER_USER_KEY: the "premium" tier (UPS + FedEx only). Greg's account, or
#   eventually a Delivery Group of users who don't want Amazon/USPS spam.
# PUSHOVER_KEY_ALL: the "all-carriers" tier (UPS, FedEx, Amazon, USPS). A
#   Delivery Group key for neighbors who want everything. Leave empty to disable.
# Both accept user keys or group keys interchangeably (Pushover's API doesn't
# distinguish). UPS/FEDEX events fan out to BOTH keys; AMAZON/USPS events go
# to the "all" key only.
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_KEY_ALL = os.environ.get("PUSHOVER_KEY_ALL", "")
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

# Carrier classification
TRACKED_CARRIERS = {"UPS", "FEDEX", "AMAZON", "USPS"}
PREMIUM_CARRIERS = {"UPS", "FEDEX"}  # also delivered to PUSHOVER_USER_KEY tier
NON_TRACKED = {"NONE", "OTHER"}      # no notification; rate-limited disk save
ALL_VERDICTS = TRACKED_CARRIERS | NON_TRACKED

CARRIER_MESSAGES = {
    "UPS":    "UPS truck spotted! Go grab your package!",
    "FEDEX":  "FedEx truck spotted! Go grab your package!",
    "AMAZON": "Amazon van spotted! Go grab your package!",
    "USPS":   "USPS truck spotted! Mail or package incoming.",
}

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


# Stage 1: cheap, calibrated binary gate. The multi-carrier prompt below
# hallucinates a carrier ~40% of the time on glare/noise scenes (Apr 26 incident);
# this binary form held 0% on the same frame across 40 trials. We only invoke
# the carrier classifier *after* two binary YES in a row.
BINARY_PROMPT = (
    "Is there a UPS, FedEx, Amazon, or USPS delivery vehicle clearly "
    "visible in this image? ONLY say YES if you can clearly see one of "
    "these carriers' branding (UPS shield, FedEx wordmark, Amazon smile, "
    "or USPS markings). Say NO for unmarked vans, passenger cars, SUVs, "
    "and anything else. Reply YES or NO only."
)

# Stage 2: only invoked after 2/2 binary confirm. Routes a confirmed delivery
# to the right notification tier.
CARRIER_PROMPT = (
    "Look at this image. Identify the most prominent delivery vehicle, if any. "
    "Reply with ONE word from this list:\n"
    "- UPS (brown truck/van with UPS branding)\n"
    "- FEDEX (white truck/van with FedEx branding, purple/orange wordmark)\n"
    "- AMAZON (dark blue Sprinter or ProMaster van with Amazon smile or Prime logo)\n"
    "- USPS (white postal truck with USPS branding)\n"
    "- OTHER (delivery-purpose truck/van without identifiable carrier)\n"
    "- NONE (no delivery vehicle; parked passenger cars do not count)\n"
    "Reply with ONLY one word from the list."
)


def _gemini_call(image_path, prompt, alarm_s=25):
    """Shared HTTP path for Gemini classification. Returns the raw upper-cased
    response, or None on failure (timeout / 3 retries exhausted).

    Hard SIGALRM backstop prevents a stalled HTTP call from blocking long
    enough to trigger the systemd watchdog (1 min). alarm_s=25 for primary
    polls, alarm_s=15 for confirm polls (worst-case must stack under 60s:
    25 binary + 5 sleep + 15 binary-confirm + 15 carrier = 60s).
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
                                {"type": "text", "text": prompt},
                            ],
                        }],
                    },
                    timeout=(10, 20),
                )
                resp.raise_for_status()
                return (resp.json()["choices"][0]["message"].get("content") or "").strip().upper()
            except TimeoutError:
                raise
            except Exception as e:
                if attempt < 2:
                    log.warning("Gemini attempt %d failed: %s", attempt + 1, e)
                    time.sleep(1)
                else:
                    log.error("Gemini failed after 3 attempts")
                    return None
    except TimeoutError:
        log.error("Gemini hard timeout (%ds)", alarm_s)
        return None
    finally:
        signal.alarm(0)


def is_delivery_truck(image_path, alarm_s=25):
    """Stage 1 binary gate. True iff Gemini sees a tracked-carrier delivery
    vehicle. Failures (timeout, network) return False — the systemd watchdog
    and healthcheck will catch sustained outages."""
    raw = _gemini_call(image_path, BINARY_PROMPT, alarm_s)
    if raw is None:
        return False
    is_yes = "YES" in raw
    log.info("Gate: %r -> %s", raw[:30], "YES" if is_yes else "NO")
    ping_healthcheck()  # successful call = pipeline healthy
    return is_yes


def classify_carrier(image_path, alarm_s=15):
    """Stage 2 carrier classifier — only invoked after 2/2 binary YES.
    Returns one of UPS/FEDEX/AMAZON/USPS/OTHER/NONE. NONE on failure."""
    raw = _gemini_call(image_path, CARRIER_PROMPT, alarm_s)
    if raw is None:
        return "NONE"
    carrier = next((v for v in ALL_VERDICTS if v in raw), "NONE")
    log.info("Carrier: %r -> %s", raw[:30], carrier)
    return carrier


_last_no_save = 0

def save_detection(image_path, carrier, suffix=""):
    """Save tracked-carrier frames always (UPS, FEDEX, AMAZON, USPS); rate-limit
    NONE/OTHER background frames to NO_SAMPLE_INTERVAL to reduce SD card wear.
    Suffixed frames (tentative/confirm) always save — they're tied to a
    detection event, not the rolling background sample. Filename format:
    <CARRIER>_<ts>[_suffix].jpg
    Returns True if saved, False if skipped."""
    global _last_no_save
    now = time.time()
    if not suffix and carrier in NON_TRACKED and (now - _last_no_save) < NO_SAMPLE_INTERVAL:
        return False  # rate-limited background sample

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    shutil.copy(image_path, os.path.join(LOG_DIR, f"{carrier}_{ts}{suffix}.jpg"))
    if not suffix and carrier in NON_TRACKED:
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


def send_notification(image_path, carrier):
    """Route notifications by carrier:
      - UPS, FEDEX  → premium tier (PUSHOVER_USER_KEY) + all tier (PUSHOVER_KEY_ALL)
      - AMAZON, USPS → all tier only
      - other carriers → no notification

    Cooldown applies globally (one notification per COOLDOWN window regardless
    of carrier — we don't want a UPS+Amazon back-to-back to ping twice).
    """
    global last_notification_time

    if not PUSHOVER_APP_TOKEN:
        log.warning("Pushover not configured (no app token)")
        return False

    now = time.time()
    if now - last_notification_time < COOLDOWN:
        log.info("Cooldown active (%ds left)", int(COOLDOWN - (now - last_notification_time)))
        return False

    targets = []
    if carrier in PREMIUM_CARRIERS and PUSHOVER_USER_KEY:
        targets.append(("premium", PUSHOVER_USER_KEY))
    if carrier in TRACKED_CARRIERS and PUSHOVER_KEY_ALL:
        targets.append(("all", PUSHOVER_KEY_ALL))

    if not targets:
        log.info("No Pushover targets for carrier=%s — skipping notification", carrier)
        return False

    message = CARRIER_MESSAGES.get(carrier, f"{carrier} delivery vehicle spotted!")
    sent_any = False
    for tier_name, key in targets:
        try:
            with open(image_path, "rb") as f:
                resp = requests.post(
                    "https://api.pushover.net/1/messages.json",
                    data={
                        "token": PUSHOVER_APP_TOKEN,
                        "user": key,
                        "message": message,
                        "title": "Delivery Truck Alert",
                        "priority": 1,
                        "sound": "siren",
                    },
                    files={"attachment": ("truck.jpg", f, "image/jpeg")},
                    timeout=10,
                )
            if resp.status_code != 200:
                log.error("Pushover %s tier returned %d: %s", tier_name, resp.status_code, resp.text[:200])
            else:
                log.info("Notification sent to %s tier (%s)", tier_name, carrier)
                sent_any = True
        except Exception as e:
            log.error("Pushover %s tier failed: %s", tier_name, e)

    if sent_any:
        last_notification_time = now
    return sent_any


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
    log.info("Two-stage: binary gate (%s) -> carrier classifier on 2/2 confirm",
             "+".join(sorted(TRACKED_CARRIERS)))
    log.info("Notifies premium tier (%s) for: %s", "set" if PUSHOVER_USER_KEY else "EMPTY", ", ".join(sorted(PREMIUM_CARRIERS)))
    log.info("Notifies all tier (%s) for: %s", "set" if PUSHOVER_KEY_ALL else "EMPTY", ", ".join(sorted(TRACKED_CARRIERS)))
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

            # Stage 1: cheap, calibrated binary gate.
            tentative_yes = is_delivery_truck(tmp_path)

            if tentative_yes:
                # Tentative YES — could be a truck driving past on the
                # cross-street. Wait 5s and re-check: a real delivery is still
                # parked, a drive-by is long gone.
                save_detection(tmp_path, "YES", suffix="_tentative")
                log.info("Tentative YES — confirming in 5s...")
                time.sleep(5)
                try:
                    grab_frame(rtsp_url, tmp_path)
                    confirm_yes = is_delivery_truck(tmp_path, alarm_s=15)
                    if confirm_yes:
                        # 2/2 binary YES — only now do we burn the carrier
                        # classifier call to route to the right tier.
                        carrier = classify_carrier(tmp_path, alarm_s=15)
                        save_detection(tmp_path, carrier, suffix="_confirm")
                        if carrier in TRACKED_CARRIERS:
                            clear_streak = 0
                            log.info(">>> DELIVERY CONFIRMED (2/2 binary), carrier=%s", carrier)
                            send_notification(tmp_path, carrier)
                        else:
                            # Binary said YES twice but classifier disagreed —
                            # ambiguous, skip to avoid wrong-carrier ping.
                            log.info("2/2 binary YES but carrier=%s — skipping notification", carrier)
                    else:
                        save_detection(tmp_path, "NO", suffix="_confirm")
                        log.info("Drive-by dismissed — YES then NO after 5s")
                except Exception as e:
                    log.warning("Confirm step failed: %s — skipping notification", e)
            else:
                # Background sample, rate-limited inside save_detection.
                save_detection(tmp_path, "NONE")
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
