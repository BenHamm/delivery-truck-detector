# Delivery Truck Detector

Detect UPS/FedEx/Amazon trucks stopped outside Greg's building via an IP camera and send push notifications so people can grab their package before the driver leaves.

> **If any doc, note, or session claims this is "single-stage Gemini", "Gemini-only", or "Pi-only" — it is stale.**
> Since ~May 2026 Stage 1 runs on the Orin's local Qwen3-VL. Verify with the Orin's `journalctl -u qwen3vl`
> (the Pi, `100.73.243.128`, sends ~2,200 requests/day). This README was corrected 2026-09-26 after a stale
> version led two sessions to conclude the Orin leg was retired.

## Architecture

```
                                      ┌──────────── Stage 1 (every poll) ────────────┐
[Tapo cam] --rtsp--> [Pi Zero 2 W] ──>│ Orin qwen3vl :8080 (Qwen3-VL-8B, local, free)│──YES──┐
                                      │   └─ on any failure → Gemini Flash (fallback)│       │
                                      └──────────────────────────────────────────────┘       │
                                                                                              ▼
                                  Stage 2 (on tentative YES): Gemini 3 Flash carrier classifier
                                                                                              │
                                                                     UPS/FEDEX/AMAZON ──> Pushover
```

1. **Tapo C320WS** camera at Greg's, pointed at the street.
2. **Pi Zero 2 W** (`greg-cam-bridge`) keeps one long-lived ffmpeg reading the RTSP feed (`-vf fps=2`) into `/tmp/truck_capture.jpg`. The detector loop polls every 15 s, 08:00–20:00.
3. **Stage 1: binary "delivery-shaped vehicle?" gate on the Orin.** `llama-server` serves Qwen3-VL-8B (Q4_K_M) on `100.118.29.32:8080` (`qwen3vl.service`, config in `orin/`). The Pi reaches it over Tailscale.
   - Fail-fast: 3 s connect / 18 s read / 1 attempt / 20 s hard alarm.
   - On any failure it falls back to Gemini Flash via OpenRouter for that poll. The alert still goes out, but that poll costs money.
   - **This service is shared** with the yard cam (`~/tapo/raccoon_watch.py` on the Orin). Don't retire or degrade it without both owners.
4. **Stage 2: carrier classifier (Gemini-only).** On a Stage-1 YES, the Pi saves the tentative frame, waits 2 s, grabs a fresh frame, and asks Gemini 3 Flash for `UPS`/`FEDEX`/`AMAZON`/`USPS`/`OTHER`/`NONE`. The ~6 s gap is the drive-by filter: a truck passing on the cross street is gone by the second frame, while a parked delivery still classifies as its carrier.
5. **Pushover routing.** One app token and two Delivery Groups on Ben's Pushover account:
   - `UPS` / `FEDEX` → premium group (`PUSHOVER_USER_KEY`, currently Greg + Maisie) **and** the all-carriers group
   - `AMAZON` → all-carriers group only (`PUSHOVER_KEY_ALL`, currently Ben)
   - `USPS` / `OTHER` / `NONE` → no notification (postal carriers have building access)
   - Priority 1, "siren" sound, photo attached, one global 10-minute cooldown (resets after 3 clear polls)

Recipients are managed as Pushover group membership, not in code. Adding or moving someone needs no deploy.

## Monitoring (Pi)

| Unit | Cadence | What it does |
|---|---|---|
| healthchecks.io ping | from `detector.py` | Dead-man's switch; alerts if the Pi goes silent |
| `canary.timer` | hourly | Stage 1: one random YES + one NO frame against the Orin. Stage 2: one carrier-labelled frame through `classify_carrier` |
| `watchdog.timer` | 5 min | `HIGH_FALLBACK` (>10% over ≥60 calls), `HIGH_TRACKED`, `SHADOW_DISAGREE`, `LOW_DISK` (<10%), `SILENCE` (≥7 expected-but-missing notifications; see the derivation in `pi/watchdog.py`). Pages **Ben only** via Pushover, rate-limited per condition. `python3 /home/pi/watchdog.py --ack` resets SILENCE. |
| `frame-health.timer` | 5 min | Forensics only: logs frame geometry and 4-corner luma plus the capture ffmpeg's pid/start time to `/home/pi/logs/frame-health.log` |
| ffmpeg stderr | continuous | `/home/pi/logs/ffmpeg-capture.log` (`+level+warning`, one banner per spawn) |

Pairing the last two is how a capture problem gets traced: `frame-health.log` gives the 5-minute window and the ffmpeg pid, and `ffmpeg-capture.log` shows what that process reported. Example: the Sep 22 2026 incident, where the long-lived ffmpeg started writing rotated frames mid-run.

## Archival

- `/home/pi/detections/`: every saved frame, 36 h retention. Archival is best-effort: a failed write logs a warning and never blocks detection (see the Sep 3 2026 ENOSPC note in `detector.py`).
- `ingest-trace.timer` (20:01 nightly) builds `/home/pi/traces/<date>.tar.gz` (full day, frames plus `trace.jsonl`) and `<date>-distilled.tar.gz` (only the tentative/confirm/carrier frames, ~1–3% of the size).
- Full tarballs keep 30 days (`RETAIN_DAYS`); distilled archives are kept indefinitely and mirrored to `eval/traces/distilled/` on the Mac.

## Cost

- **Stage 1 (Orin):** free. About 2,200 polls/day run locally instead of on a paid API.
- **OpenRouter/Gemini:** Stage 2 calls, Stage-1 fallbacks (normally <1–5% of polls), and one canary Stage-2 call per hour.
- **Pushover:** $5 one-time per phone. **Healthchecks.io / Tailscale:** free tier. **Pi:** ~1 W.

## Setup

Runtime config lives in the systemd unit environment, not a `.env` file: see `pi/detector.service.example`, plus the drop-ins on the Pi (`canary.service.d/stage2.conf`, `watchdog.service.d/paging.conf`). Orin units are in `orin/`.

```bash
sudo systemctl status detector canary.timer watchdog.timer frame-health.timer ingest-trace.timer   # Pi
systemctl status qwen3vl qwen3vl-watchdog.timer                                                    # Orin
```

## History

1. **Jetson era:** the Pi forwarded frames to a Jetson running YOLOv8 + TensorRT.
2. **Roboflow + Gemini:** when the Jetson was reassigned, Stage 1 moved to Roboflow with Gemini as Stage 2.
3. **Single-stage Gemini (Apr 22 2026):** the Roboflow free tier ran out, so Gemini did everything for a few weeks.
4. **Orin Qwen3-VL Stage 1 (~May 2026 to present):** the current design above. Stage 1 moved back to local hardware, on the Orin's Qwen3-VL. About 2,200 polls a day now run locally. Gemini handles only Stage 2, Stage-1 fallbacks, and the canary: roughly 100–300 calls a day against ~2,500 if it did everything, so on the order of 10–25× fewer paid calls. Gemini became the Stage-1 fallback plus the Stage-2 classifier.

An early Orin cron health check (`/home/orin/truck-detector/healthcheck.sh`) was retired in favour of healthchecks.io. Its crontab line may linger, but the script no longer exists. `jetson/` and `laptop/` are historical artifacts. **`orin/` is active.**
