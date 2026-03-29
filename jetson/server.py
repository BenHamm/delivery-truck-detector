#!/usr/bin/env python3
"""
Delivery truck detection server for Jetson Orin AGX.

Two-stage pipeline:
  1. YOLOv8n (TensorRT) detects COCO truck/bus classes (delivery trucks
     register as either depending on angle)
  2. Multimodal LLM (Kimi K2.5 via OpenRouter) confirms whether it's
     actually a delivery truck

Uses TensorRT + pycuda directly — no PyTorch or torchvision needed.
"""

import io
import os
import time
import base64
import logging
import argparse
import subprocess
import threading
from pathlib import Path

import numpy as np
from PIL import Image
import tensorrt as trt
import pycuda.driver as cuda
import pycuda.autoinit  # noqa: F401

import requests
from flask import Flask, request as flask_request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

YOLO_MODEL_PATH = os.environ.get("YOLO_MODEL_PATH", "yolov8n.engine")
INPUT_SIZE = 640
# COCO classes: 5=bus, 7=truck — delivery trucks register as either
VEHICLE_CLASS_IDS = {5, 7}
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.20"))
NMS_IOU_THRESHOLD = float(os.environ.get("NMS_IOU_THRESHOLD", "0.45"))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3-flash-preview")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN = os.environ.get("PUSHOVER_APP_TOKEN", "")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "600"))
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "snapshots")
PORT = int(os.environ.get("PORT", "5555"))

RTSP_URL = os.environ.get("RTSP_URL", "")
RTSP_INTERVAL = int(os.environ.get("RTSP_INTERVAL", "3"))

# ---------------------------------------------------------------------------
# TensorRT engine wrapper
# ---------------------------------------------------------------------------

TRT_LOGGER = trt.Logger(trt.Logger.WARNING)


class TRTInference:
    """Minimal TensorRT inference wrapper for YOLOv8n."""

    def __init__(self, engine_path):
        log.info("Loading TensorRT engine: %s", engine_path)
        with open(engine_path, "rb") as f:
            runtime = trt.Runtime(TRT_LOGGER)
            self.engine = runtime.deserialize_cuda_engine(f.read())
        self.context = self.engine.create_execution_context()

        self.inputs = []
        self.outputs = []
        self.bindings = []
        self.stream = cuda.Stream()

        for i in range(self.engine.num_bindings):
            dtype = trt.nptype(self.engine.get_binding_dtype(i))
            shape = self.engine.get_binding_shape(i)
            size = trt.volume(shape)
            host_mem = cuda.pagelocked_empty(size, dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            self.bindings.append(int(device_mem))

            if self.engine.binding_is_input(i):
                self.inputs.append({"host": host_mem, "device": device_mem, "shape": shape})
            else:
                self.outputs.append({"host": host_mem, "device": device_mem, "shape": shape})

        log.info("Engine loaded — output shape: %s", self.outputs[0]["shape"])

    def infer(self, input_array):
        np.copyto(self.inputs[0]["host"], input_array.ravel())
        cuda.memcpy_htod_async(self.inputs[0]["device"], self.inputs[0]["host"], self.stream)
        self.context.execute_async_v2(bindings=self.bindings, stream_handle=self.stream.handle)
        for out in self.outputs:
            cuda.memcpy_dtoh_async(out["host"], out["device"], self.stream)
        self.stream.synchronize()
        return self.outputs[0]["host"].reshape(self.outputs[0]["shape"])


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def preprocess(image, input_size=INPUT_SIZE):
    iw, ih = image.size
    scale = min(input_size / iw, input_size / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    image_resized = image.resize((nw, nh), Image.BILINEAR)

    canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
    pad_x, pad_y = (input_size - nw) // 2, (input_size - nh) // 2
    canvas.paste(image_resized, (pad_x, pad_y))

    arr = np.array(canvas, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)
    arr = np.expand_dims(arr, 0)
    return np.ascontiguousarray(arr), scale, pad_x, pad_y


# ---------------------------------------------------------------------------
# Postprocessing: NMS in pure numpy
# ---------------------------------------------------------------------------

def nms_numpy(boxes, scores, iou_threshold):
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []

    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(iou <= iou_threshold)[0] + 1]

    return keep


def postprocess(output, conf_threshold, iou_threshold, scale, pad_x, pad_y):
    """
    Parse YOLOv8 output for truck/bus detections.
    Output shape: (1, 84, 8400) — 4 bbox coords + 80 class scores per anchor.
    """
    preds = output[0].T  # (8400, 84)
    boxes_cxcywh = preds[:, :4]
    class_scores = preds[:, 4:]

    # Take max score across truck and bus classes for each anchor
    vehicle_scores = class_scores[:, list(VEHICLE_CLASS_IDS)].max(axis=1)
    mask = vehicle_scores > conf_threshold

    if not np.any(mask):
        return []

    boxes_cxcywh = boxes_cxcywh[mask]
    scores = vehicle_scores[mask]

    # cx,cy,w,h -> x1,y1,x2,y2
    boxes = np.zeros_like(boxes_cxcywh)
    boxes[:, 0] = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
    boxes[:, 1] = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
    boxes[:, 2] = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
    boxes[:, 3] = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2

    keep = nms_numpy(boxes, scores, iou_threshold)
    boxes = boxes[keep]
    scores = scores[keep]

    # Map back to original image coordinates
    boxes[:, 0] = (boxes[:, 0] - pad_x) / scale
    boxes[:, 1] = (boxes[:, 1] - pad_y) / scale
    boxes[:, 2] = (boxes[:, 2] - pad_x) / scale
    boxes[:, 3] = (boxes[:, 3] - pad_y) / scale

    return [(b[0], b[1], b[2], b[3], s) for b, s in zip(boxes, scores)]


# ---------------------------------------------------------------------------
# Stage 2: LLM confirmation
# ---------------------------------------------------------------------------

def confirm_with_llm(image_bytes, retries=2):
    """Ask LLM whether this is a delivery truck. Returns True/False."""
    if not OPENROUTER_API_KEY:
        log.warning("No OPENROUTER_API_KEY — skipping LLM, treating as positive")
        return True

    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "model": OPENROUTER_MODEL,
        "max_tokens": 20,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Is there a UPS or FedEx delivery truck clearly "
                            "visible in this image? ONLY say YES for vehicles "
                            "with UPS or FedEx branding. Say NO for all other "
                            "vehicles including Amazon, USPS, unmarked vans, "
                            "and anything else. "
                            "Reply YES or NO only."
                        ),
                    },
                ],
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(retries + 1):
        try:
            resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            answer = (msg.get("content") or "").strip().upper()
            is_delivery = "YES" in answer
            log.info("LLM confirmation: %s -> %s", answer, "DELIVERY" if is_delivery else "NOT DELIVERY")
            return is_delivery
        except Exception as e:
            if attempt < retries:
                log.warning("LLM attempt %d failed: %s — retrying", attempt + 1, e)
                time.sleep(1)
            else:
                log.error("LLM failed after %d attempts: %s — treating as positive", retries + 1, e)
                return True


# ---------------------------------------------------------------------------
# Detection pipeline
# ---------------------------------------------------------------------------

app = Flask(__name__)
trt_engine = None
last_notification_time = 0


def detect_vehicle(image_bytes):
    """Stage 1: YOLO truck/bus detection. Returns (detected, best_conf, detections)."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    blob, scale, pad_x, pad_y = preprocess(image)
    output = trt_engine.infer(blob)
    detections = postprocess(output, CONFIDENCE_THRESHOLD, NMS_IOU_THRESHOLD, scale, pad_x, pad_y)

    if detections:
        best = max(detections, key=lambda d: d[4])
        log.info("YOLO: vehicle detected — %d box(es), best conf=%.2f", len(detections), best[4])
        return True, best[4], detections
    return False, 0.0, []


def save_snapshot(image_bytes, confirmed):
    Path(SNAPSHOT_DIR).mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    tag = "delivery" if confirmed else "vehicle"
    path = Path(SNAPSHOT_DIR) / f"{tag}_{ts}.jpg"
    path.write_bytes(image_bytes)
    log.info("Snapshot saved: %s", path)
    return str(path)


def send_notification(image_bytes, confidence):
    global last_notification_time

    now = time.time()
    if now - last_notification_time < COOLDOWN_SECONDS:
        log.info("Cooldown active (%ds remaining)", int(COOLDOWN_SECONDS - (now - last_notification_time)))
        return False

    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        log.warning("Pushover not configured")
        return False

    try:
        resp = requests.post(
            PUSHOVER_URL,
            data={
                "token": PUSHOVER_APP_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "message": f"Delivery truck spotted (confidence: {confidence:.0%}). Go grab your package!",
                "title": "Delivery Truck Alert",
                "priority": 1,
                "sound": "siren",
            },
            files={
                "attachment": ("truck.jpg", io.BytesIO(image_bytes), "image/jpeg"),
            },
            timeout=10,
        )
        resp.raise_for_status()
        last_notification_time = now
        log.info("Notification sent")
        return True
    except Exception as e:
        log.error("Notification failed: %s", e)
        return False


def process_frame(image_bytes):
    """Full two-stage pipeline for one frame."""
    # Stage 1: YOLO
    detected, confidence, detections = detect_vehicle(image_bytes)
    if not detected:
        return {"status": "no_vehicle"}

    # Stage 2: LLM confirmation
    is_delivery = confirm_with_llm(image_bytes)
    save_snapshot(image_bytes, confirmed=is_delivery)

    if not is_delivery:
        log.info("LLM says not a delivery truck — skipping notification")
        return {"status": "not_delivery", "yolo_confidence": round(confidence, 3)}

    sent = send_notification(image_bytes, confidence)
    return {
        "status": "notified" if sent else "cooldown",
        "confidence": round(confidence, 3),
        "num_detections": len(detections),
    }


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@app.route("/upload", methods=["POST"])
def upload():
    if "image" not in flask_request.files:
        return jsonify({"error": "no image"}), 400
    image_bytes = flask_request.files["image"].read()
    return jsonify(process_frame(image_bytes)), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": YOLO_MODEL_PATH}), 200


# ---------------------------------------------------------------------------
# Local RTSP poller
# ---------------------------------------------------------------------------

def rtsp_poll_loop(rtsp_url, interval):
    log.info("Starting RTSP poller: %s (every %ds)", rtsp_url, interval)
    tmp_path = "/tmp/truck_frame.jpg"

    while True:
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-rtsp_transport", "tcp",
                 "-i", rtsp_url, "-frames:v", "1", "-q:v", "5", tmp_path],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                image_bytes = Path(tmp_path).read_bytes()
                res = process_frame(image_bytes)
                if res["status"] != "no_vehicle":
                    log.info("RTSP result: %s", res)
            else:
                log.warning("ffmpeg: %s", result.stderr.decode()[-200:])
        except Exception as e:
            log.error("RTSP poll error: %s", e)

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global trt_engine, CONFIDENCE_THRESHOLD

    parser = argparse.ArgumentParser(description="Delivery truck detection server")
    parser.add_argument("--model", default=YOLO_MODEL_PATH, help="Path to YOLO .engine file")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--rtsp", default=RTSP_URL)
    parser.add_argument("--rtsp-interval", type=int, default=RTSP_INTERVAL)
    parser.add_argument("--confidence", type=float, default=CONFIDENCE_THRESHOLD)
    parser.add_argument("--test", nargs="*",
                        help="Test mode: run detection + LLM on images and exit")
    args = parser.parse_args()

    CONFIDENCE_THRESHOLD = args.confidence
    trt_engine = TRTInference(args.model)

    if args.test is not None:
        for img_path in args.test:
            image_bytes = Path(img_path).read_bytes()
            detected, conf, dets = detect_vehicle(image_bytes)
            if detected:
                is_delivery = confirm_with_llm(image_bytes)
                label = "DELIVERY" if is_delivery else "vehicle"
                print(f"{label:8s} (yolo={conf:.2f}, boxes={len(dets)})  {img_path}")
            else:
                print(f"{'clear':8s} (yolo={conf:.2f}, boxes={len(dets)})  {img_path}")
        return

    if args.rtsp:
        threading.Thread(target=rtsp_poll_loop, args=(args.rtsp, args.rtsp_interval), daemon=True).start()

    log.info("Starting server on port %d", args.port)
    app.run(host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
