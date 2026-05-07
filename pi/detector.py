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

# Orin local Stage 1 (Qwen3-VL-8B via llama-server, OpenAI-compatible).
# Optional: when ORIN_BASE_URL is set, Stage 1 polls hit Orin first and
# fall back to OpenRouter only if Orin times out or errors. When unset
# (default), behavior is identical to the all-Gemini pipeline.
# Validated architecture in eval: Qwen Stage 1 (permissive prompt)
# + Gemini Stage 2 = 24/25 vs all-Gemini baseline 23/25.
ORIN_BASE_URL = os.environ.get("ORIN_BASE_URL", "")  # e.g. http://100.118.29.32:8080/v1
ORIN_MODEL = os.environ.get("ORIN_MODEL", "qwen3-vl")  # llama-server ignores name but it goes in the request

# Append-only audit log of every notification attempt (sent / cooldown /
# skipped / failed). Persistent independent of journald rotation so we can
# always answer "did notifications fire today?" by reading this file.
AUDIT_LOG = os.environ.get("NOTIFY_AUDIT_LOG", "/home/pi/notifications.log")

# Shadow mode: when enabled, every Orin Stage 1 call is mirrored against
# Gemini (with the restrictive prompt) for parity comparison. Disagreements
# are logged loudly so we can see real-traffic gaps the eval suite missed.
# Costs an extra ~$0.001 per poll. Default off; turn on for 24-48h after a
# config change, then turn off.
SHADOW_GEMINI = os.environ.get("SHADOW_GEMINI", "").strip() in ("1", "true", "yes")

# Pushover — two recipient tiers
# PUSHOVER_USER_KEY: the "premium" tier (UPS + FedEx only). Greg's account, or
#   eventually a Delivery Group of users who don't want Amazon spam.
# PUSHOVER_KEY_ALL: the "all-carriers" tier (UPS, FedEx, Amazon). A Delivery
#   Group key for neighbors who want everything. Leave empty to disable.
# Both accept user keys or group keys interchangeably (Pushover's API doesn't
# distinguish). UPS/FEDEX events fan out to BOTH keys; AMAZON events go to
# the "all" key only. USPS is excluded entirely — postal carriers have
# building access, so the alert would be useless.
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
NO_SAMPLE_INTERVAL = int(os.environ.get("NO_SAMPLE_INTERVAL", "25"))  # ~25s threshold: save every poll (polls run every ~36s), so every tentative YES has a NO frame from 30s prior for lead-up audit

# Healthchecks.io dead-man's switch
HC_PING_URL = os.environ.get("HC_PING_URL", "https://hc-ping.com/1d4cb30e-1d3e-4425-b6d9-f1f93590ca4c")
HC_INTERVAL = 1800  # ping every 30 minutes

# Carrier classification
# USPS is intentionally absent from TRACKED_CARRIERS: postal carriers have
# building access (mail goes directly to the boxes), so a notification would
# be useless. We still ID USPS in the classifier so we don't *misclassify*
# it as another carrier and notify wrongly.
TRACKED_CARRIERS = {"UPS", "FEDEX", "AMAZON"}
PREMIUM_CARRIERS = {"UPS", "FEDEX"}            # also delivered to PUSHOVER_USER_KEY tier
NON_TRACKED = {"NONE", "OTHER", "USPS"}        # detected but no notification; rate-limited disk save
ALL_VERDICTS = TRACKED_CARRIERS | NON_TRACKED

CARRIER_MESSAGES = {
    "UPS":    "UPS truck spotted! Go grab your package!",
    "FEDEX":  "FedEx truck spotted! Go grab your package!",
    "AMAZON": "Amazon van spotted! Go grab your package!",
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
# this binary form held 0% on the same frame across 40 trials. The carrier
# classifier is only invoked on a fresh frame +5s after a tentative binary YES,
# and acts as both the confirmation gate (drive-by filter) and the routing key.
BINARY_PROMPT = (
    "Is there a UPS, FedEx, or Amazon delivery vehicle clearly "
    "visible in this image? ONLY say YES if you can clearly see one of "
    "these carriers' branding (UPS shield, FedEx wordmark, or Amazon "
    "smile/Prime logo). Say NO for USPS trucks, unmarked vans, passenger "
    "cars, SUVs, and anything else. Reply YES or NO only."
)

# Permissive Stage 1 prompt used ONLY against Orin/Qwen. Smaller open-weight
# models can spot delivery-shaped vehicles reliably but can't be trusted to
# distinguish carrier branding precisely; we let Stage 2 (Gemini) make that
# call. Validated against the 25-case eval at 24/25 with this exact prompt.
BINARY_PROMPT_PERMISSIVE = (
    "Is there any delivery-shaped vehicle visible in this image? A "
    "delivery-shaped vehicle is a large van, step van, box truck, or "
    "cargo van -- of the kind used by carriers like UPS, FedEx, Amazon, "
    "USPS, or other commercial delivery services. Branding is NOT "
    "required: an unmarked white or dark van the size of a Sprinter "
    "still counts. Reply YES if you see at least one such vehicle. "
    "Reply NO only if the only vehicles visible are clearly passenger "
    "cars, SUVs, sedans, pickup trucks, or motorcycles. Reply YES or NO only."
)

# Stage 2: invoked on a fresh frame 5s after a tentative binary YES. Doubles
# as both the drive-by filter (a moving truck is gone -> carrier=NONE) and
# the routing classifier (carrier in TRACKED -> notify the right tier).
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


def _openai_compatible_call(base_url, api_key, model, image_b64, prompt,
                            connect_timeout, read_timeout, retries=3):
    """Generic OpenAI-compatible /chat/completions call. Returns raw
    upper-cased content string, or None on failure (TimeoutError propagates).
    Used for both OpenRouter (Gemini) and a local Orin llama-server."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    for attempt in range(retries):
        try:
            resp = requests.post(
                base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": 20,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {
                                "url": "data:image/jpeg;base64," + image_b64}},
                            {"type": "text", "text": prompt},
                        ],
                    }],
                },
                timeout=(connect_timeout, read_timeout),
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"].get("content") or "").strip().upper()
        except TimeoutError:
            raise
        except Exception as e:
            if attempt < retries - 1:
                log.warning("LLM call attempt %d failed (%s): %s",
                            attempt + 1, base_url, e)
                time.sleep(1)
            else:
                log.error("LLM call failed after %d attempts (%s): %s",
                          retries, base_url, e)
                return None


def _gemini_call(image_path, prompt, alarm_s=25):
    """Shared OpenRouter/Gemini path. Returns the raw upper-cased response,
    or None on failure. Hard SIGALRM backstop prevents a stalled HTTP call
    from triggering the systemd watchdog. alarm_s=25 for primary binary
    polls, alarm_s=15 for the carrier confirm."""
    def _alarm_handler(signum, frame):
        raise TimeoutError("Gemini hard timeout")

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(alarm_s)
    try:
        return _openai_compatible_call(
            "https://openrouter.ai/api/v1", OPENROUTER_API_KEY,
            OPENROUTER_MODEL, b64, prompt,
            connect_timeout=10, read_timeout=20, retries=3,
        )
    except TimeoutError:
        log.error("Gemini hard timeout (%ds)", alarm_s)
        return None
    finally:
        signal.alarm(0)


def _orin_call(image_path, prompt, alarm_s=20):
    """Stage 1 path against the local Orin llama-server. Returns the raw
    upper-cased response, or None on any failure (caller falls back to
    Gemini). Smoke-tested at ~11s per inference on a Jetson AGX Orin with
    Qwen3-VL-8B Q4_K_M; alarm_s=20 leaves comfortable headroom while still
    failing fast if the Orin is genuinely hung."""
    if not ORIN_BASE_URL:
        return None

    def _alarm_handler(signum, frame):
        raise TimeoutError("Orin hard timeout")

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(alarm_s)
    try:
        return _openai_compatible_call(
            ORIN_BASE_URL, "", ORIN_MODEL, b64, prompt,
            connect_timeout=3, read_timeout=18, retries=1,  # fail fast
        )
    except TimeoutError:
        log.warning("Orin hard timeout (%ds) -- falling back to Gemini", alarm_s)
        return None
    finally:
        signal.alarm(0)


def is_delivery_truck(image_path, alarm_s=25):
    """Stage 1 binary gate. Try Orin (Qwen3-VL-8B local, permissive prompt)
    first; on any failure, fall back to Gemini (restrictive prompt). When
    ORIN_BASE_URL is unset, this is identical to the all-Gemini path.

    Failures of BOTH backends return False -- the systemd watchdog and
    healthcheck catch sustained outages.

    When SHADOW_GEMINI is enabled, every successful Orin call is mirrored
    against Gemini and disagreements are logged. The Orin verdict still
    drives the actual firing decision; Gemini only watches."""
    if ORIN_BASE_URL:
        raw = _orin_call(image_path, BINARY_PROMPT_PERMISSIVE)
        if raw is not None:
            is_yes = "YES" in raw
            log.info("Gate (orin): %r -> %s", raw[:30], "YES" if is_yes else "NO")
            ping_healthcheck()
            if SHADOW_GEMINI:
                shadow_raw = _gemini_call(image_path, BINARY_PROMPT, alarm_s=10)
                if shadow_raw is not None:
                    shadow_yes = "YES" in shadow_raw
                    if shadow_yes != is_yes:
                        # Disagreement: log image filename so we can review later.
                        log.warning("SHADOW DISAGREE: orin=%s gemini=%s frame=%s",
                                    "YES" if is_yes else "NO",
                                    "YES" if shadow_yes else "NO",
                                    image_path)
                    else:
                        log.info("Shadow: agree=%s", "YES" if is_yes else "NO")
                else:
                    log.warning("Shadow Gemini call failed; not counting")
            return is_yes
        log.warning("Orin Stage 1 unavailable -- falling back to Gemini")

    raw = _gemini_call(image_path, BINARY_PROMPT, alarm_s)
    if raw is None:
        return False
    is_yes = "YES" in raw
    log.info("Gate (gemini): %r -> %s", raw[:30], "YES" if is_yes else "NO")
    ping_healthcheck()  # successful call = pipeline healthy
    return is_yes


def classify_carrier(image_path, alarm_s=15):
    """Stage 2 carrier classifier. Invoked on the +5s frame after a tentative
    binary YES, doubling as drive-by filter and tier-routing key. Always
    Gemini -- our eval showed Stage 2 is hard for cheaper models.
    Returns one of UPS/FEDEX/AMAZON/USPS/OTHER/NONE. NONE on failure."""
    raw = _gemini_call(image_path, CARRIER_PROMPT, alarm_s)
    if raw is None:
        return "NONE"
    carrier = next((v for v in ALL_VERDICTS if v in raw), "NONE")
    log.info("Carrier: %r -> %s", raw[:30], carrier)
    return carrier


_last_no_save = 0

def save_detection(image_path, carrier, suffix=""):
    """Save tracked-carrier frames always (UPS, FEDEX, AMAZON); rate-limit
    NONE/OTHER/USPS background frames to NO_SAMPLE_INTERVAL.
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


def _audit(line):
    """Append-only persistent audit line. Survives journald rotation."""
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + line + "\n")
    except Exception as e:
        log.error("Failed to write audit log: %s", e)


def send_notification(image_path, carrier):
    """Route notifications by carrier:
      - UPS, FEDEX  → premium tier (PUSHOVER_USER_KEY) + all tier (PUSHOVER_KEY_ALL)
      - AMAZON      → all tier only
      - USPS / OTHER / NONE → no notification (USPS has building access)

    Cooldown applies globally (one notification per COOLDOWN window regardless
    of carrier — we don't want a UPS+Amazon back-to-back to ping twice).

    Every call writes an audit line to AUDIT_LOG with the outcome so audits
    survive journald rotation under heavy load.
    """
    global last_notification_time

    frame_name = os.path.basename(image_path)

    if not PUSHOVER_APP_TOKEN:
        log.warning("Pushover not configured (no app token)")
        _audit(f"carrier={carrier} frame={frame_name} result=no_token")
        return False

    now = time.time()
    if now - last_notification_time < COOLDOWN:
        log.info("Cooldown active (%ds left)", int(COOLDOWN - (now - last_notification_time)))
        _audit(f"carrier={carrier} frame={frame_name} result=cooldown")
        return False

    targets = []
    if carrier in PREMIUM_CARRIERS and PUSHOVER_USER_KEY:
        targets.append(("premium", PUSHOVER_USER_KEY))
    if carrier in TRACKED_CARRIERS and PUSHOVER_KEY_ALL:
        targets.append(("all", PUSHOVER_KEY_ALL))

    if not targets:
        log.info("No Pushover targets for carrier=%s — skipping notification", carrier)
        _audit(f"carrier={carrier} frame={frame_name} result=no_targets")
        return False

    message = CARRIER_MESSAGES.get(carrier, f"{carrier} delivery vehicle spotted!")
    sent_any = False
    tier_results = []
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
                tier_results.append(f"{tier_name}:{resp.status_code}")
            else:
                log.info("Notification sent to %s tier (%s)", tier_name, carrier)
                tier_results.append(f"{tier_name}:200")
                sent_any = True
        except Exception as e:
            log.error("Pushover %s tier failed: %s", tier_name, e)
            tier_results.append(f"{tier_name}:err")

    result = "sent" if sent_any else "failed"
    _audit(f"carrier={carrier} frame={frame_name} result={result} tiers={','.join(tier_results)}")

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
    log.info("Two-stage: binary gate (%s) -> carrier classifier on +5s frame",
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
                # cross-street. Wait 5s, then run the carrier classifier on
                # a fresh frame: a parked delivery still classifies as its
                # carrier, a drive-by classifies as NONE (truck moved out
                # of frame). Earlier design used a binary confirm step, but
                # binary flickered on borderline real Amazon vans (Apr 27
                # incident); carrier was rock-solid on the same frames.
                # Carrier doubles as the routing key.
                save_detection(tmp_path, "YES", suffix="_tentative")
                # Skip the explicit sleep: Orin Stage 1 takes ~11s + ffmpeg
                # adds another ~3-5s, so the confirm grab is already ~15s
                # after the tentative capture -- plenty of temporal gap
                # for drive-by filtering. The 5s wait was calibrated for
                # the old all-Gemini architecture where Stage 1 was ~2s.
                log.info("Tentative YES — grabbing confirm frame...")
                try:
                    grab_frame(rtsp_url, tmp_path)
                    carrier = classify_carrier(tmp_path, alarm_s=15)
                    save_detection(tmp_path, carrier, suffix="_confirm")
                    if carrier in TRACKED_CARRIERS:
                        clear_streak = 0
                        log.info(">>> DELIVERY CONFIRMED, carrier=%s", carrier)
                        send_notification(tmp_path, carrier)
                    else:
                        # Drive-by, untracked vehicle, or USPS — no notification.
                        log.info("Confirm carrier=%s — drive-by or untracked, skipping", carrier)
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
