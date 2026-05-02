#!/usr/bin/env python3
"""Synthetic canary for the Orin Stage 1 endpoint.

Posts two known-answer images to the local llama-server on Orin and
asserts the right verdicts. Exits 0 on success, non-zero on any mismatch
or transport error -- so this can be wired to systemd Restart, cron
mailto-on-failure, or healthchecks.io / start.

Determinism was verified: same image + temperature=0 + seed=42 returns
the same verdict 20/20 trials. Drift here means the model state is
degraded (memory pressure, thermal throttling, llama.cpp regression).

Usage:
    ORIN_BASE_URL=http://100.118.29.32:8080/v1 python3 canary.py

Frames expected at /home/pi/canary-frames/yes.jpg (a clear delivery van)
and /home/pi/canary-frames/no.jpg (an empty residential street).
"""
import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request


BASE_URL = os.environ.get("ORIN_BASE_URL", "http://100.118.29.32:8080/v1")
MODEL = os.environ.get("ORIN_MODEL", "qwen3-vl")
FRAMES_DIR = pathlib.Path(os.environ.get("CANARY_FRAMES_DIR",
                                         "/home/pi/canary-frames"))

PROMPT = (
    "Is there any delivery-shaped vehicle visible in this image? A "
    "delivery-shaped vehicle is a large van, step van, box truck, or "
    "cargo van -- of the kind used by carriers like UPS, FedEx, Amazon, "
    "USPS, or other commercial delivery services. Branding is NOT "
    "required: an unmarked white or dark van the size of a Sprinter "
    "still counts. Reply YES if you see at least one such vehicle. "
    "Reply NO only if the only vehicles visible are clearly passenger "
    "cars, SUVs, sedans, pickup trucks, or motorcycles. Reply YES or NO only."
)

CASES = [
    ("yes.jpg", "YES"),
    ("no.jpg", "NO"),
]


def query(image_path):
    img_b64 = base64.b64encode(image_path.read_bytes()).decode()
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 20,
        "temperature": 0,
        "seed": 42,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + img_b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read())
    elapsed = time.time() - t0
    raw = d["choices"][0]["message"]["content"].strip().upper()
    parsed = "YES" if "YES" in raw else "NO" if "NO" in raw else "?"
    return parsed, raw, elapsed


def main():
    failures = []
    for name, expected in CASES:
        path = FRAMES_DIR / name
        if not path.exists():
            print(f"FAIL {name}: missing canary frame at {path}", file=sys.stderr)
            failures.append(name)
            continue
        try:
            got, raw, elapsed = query(path)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            print(f"FAIL {name}: transport error: {e}", file=sys.stderr)
            failures.append(name)
            continue
        ok = got == expected
        status = "PASS" if ok else "FAIL"
        print(f"{status} {name}: expect={expected} got={got} ({elapsed:.2f}s) raw={raw!r}")
        if not ok:
            failures.append(name)
    if failures:
        print(f"\nCANARY FAIL: {len(failures)} of {len(CASES)} cases failed",
              file=sys.stderr)
        sys.exit(1)
    print(f"\nCANARY OK: {len(CASES)}/{len(CASES)} pass")


if __name__ == "__main__":
    main()
