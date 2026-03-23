# Delivery Truck Detector

Detect delivery trucks parked on the street via IP camera and get push notifications so you can grab your package before they leave.

## Architecture

```
[Tapo Camera @ Greg's] --wifi--> [Pi Zero 2 W @ Greg's] --tailscale--> [Jetson Orin AGX @ Ben's] --push--> [Greg's Phone]
```

1. **Tapo IP camera** captures video on Greg's Wi-Fi, pointed at the street
2. **Pi Zero 2 W** grabs JPEG snapshots every few seconds and forwards them over a Tailscale tunnel to Ben's Jetson
3. **Jetson Orin AGX** runs YOLOv8n via TensorRT (no PyTorch needed at runtime) — detects COCO class 7 (`truck`)
4. **Pushover** sends a push notification with the snapshot to Greg's phone

No cloud ML APIs needed. The YOLO model runs entirely on the Jetson's GPU at ~30 FPS.

## Setup

### Jetson (detection server)

```bash
cd jetson
cp .env.example .env  # edit with Pushover keys
python3 server.py

# Test with sample images:
python3 server.py --test ../sample_images/*.png

# Test with local RTSP camera:
python3 server.py --rtsp rtsp://user:pass@camera:554/stream1
```

### Pi (snapshot forwarder)

```bash
cp .env.example ~/.env  # edit with camera + Jetson details
python3 forwarder.py
```

## Cost

- **Tailscale**: Free
- **Pushover**: $5 one-time (Greg's phone)
- **Electricity**: Pi draws ~1W. Negligible.
- **No API costs** — all inference is local
