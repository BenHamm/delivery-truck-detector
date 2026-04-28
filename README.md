# Delivery Truck Detector

Detect UPS/FedEx trucks parked on the street via IP camera and get push notifications so you can grab your package before they leave.

## Architecture

```
[Tapo Camera] --rtsp--> [Pi Zero 2 W] --https--> [Gemini Flash] --push--> [Phone]
```

1. **Tapo IP camera** captures video, pointed at the street
2. **Pi Zero 2 W** grabs one JPEG every 30s via ffmpeg/RTSP
3. **Gemini Flash** (via OpenRouter) runs a cheap binary "is this a UPS/FedEx/Amazon delivery vehicle?" gate on every poll
4. On binary YES, the Pi waits 5s and re-runs the gate — drive-bys vanish, parked trucks persist. Only after 2/2 binary YES does it spend the more expensive carrier-classification call (`UPS`/`FEDEX`/`AMAZON`/`USPS`/`OTHER`/`NONE`) for routing.
5. **Pushover** sends a push notification, routed by carrier:
   - `UPS` / `FEDEX` → premium tier (`PUSHOVER_USER_KEY`) **and** all-carriers tier
   - `AMAZON` → all-carriers tier only (`PUSHOVER_KEY_ALL`)
   - `USPS` → no notification (postal carriers have building access)
   - The premium tier exists for users who don't want Amazon spam

A **healthchecks.io** dead-man's switch fires a Pushover alert if the Pi goes silent for >30 minutes.

The Pi runs everything end-to-end. No Jetson, no laptop relay, no local ML model.

## Setup

```bash
cp pi/.env.example ~/.env  # edit with your keys
sudo cp pi/detector.service.example /etc/systemd/system/detector.service
sudo systemctl enable --now detector
journalctl -u detector -f
```

## Cost

- **Tailscale** (for SSH access): Free
- **Pushover**: $5 one-time per phone
- **Gemini Flash via OpenRouter**: ~$1.50/month (one classification per 30s during 8am–8pm)
- **Healthchecks.io**: Free tier
- **Electricity**: Pi draws ~1W

## Operational notes

- Active hours default 8:00–20:00 (configurable via `START_HOUR`/`END_HOUR`)
- Post-notification cooldown (default 10 min) prevents spam from the same parked truck
- Frames saved to `/home/pi/detections/` with 36h retention. YES frames always saved (with `_tentative` and `_confirm` suffixes for the 2-stage pair); NO frames sampled every 30 min for SD card health
- Hard 25s SIGALRM backstop on every Gemini call — a stalled HTTP request can't block the loop indefinitely

## History

Originally a Pi-forwards-to-Jetson architecture using YOLOv8 + TensorRT for local inference. Migrated to a stage-1 Roboflow + stage-2 Gemini design when the Jetson got reassigned, then collapsed to single-stage Gemini after the Roboflow free tier was exhausted. The `jetson/` and `laptop/` directories are kept as historical artifacts.
