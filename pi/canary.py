#!/usr/bin/env python3
"""Synthetic canary for the Orin Stage 1 endpoint.

Picks ONE random YES image and ONE random NO image from the rotation
set in /home/pi/canary-frames/, posts both to llama-server, asserts
the right verdicts. The random selection forces a cache miss on every
run, which means the full inference path (vision encoder + prefill +
decode) is exercised -- so we'd notice if any of those layers silently
degraded. A fixed-image canary would only test the decode path with
KV cache hits, missing vision-encoder failures.

Exits 0 on pass, 1 on any mismatch/transport error -- wire to systemd
Restart, healthchecks.io, etc.

Usage:
    ORIN_BASE_URL=http://100.118.29.32:8080/v1 python3 canary.py

Frame conventions in CANARY_FRAMES_DIR:
    yes_*.jpg  - any image that should produce YES (real delivery vehicle)
    no_*.jpg   - any image that should produce NO (no delivery vehicle)
"""
import base64
import json
import os
import pathlib
import random
import sys
import time
import urllib.error
import urllib.request


BASE_URL = os.environ.get("ORIN_BASE_URL", "http://100.118.29.32:8080/v1")
MODEL = os.environ.get("ORIN_MODEL", "qwen3-vl")
FRAMES_DIR = pathlib.Path(os.environ.get("CANARY_FRAMES_DIR",
                                         "/home/pi/canary-frames"))

PROMPT_BASE = (
    "Is there any delivery-shaped vehicle visible in this image? A "
    "delivery-shaped vehicle is a large van, step van, box truck, or "
    "cargo van -- of the kind used by carriers like UPS, FedEx, Amazon, "
    "USPS, or other commercial delivery services. Branding is NOT "
    "required: an unmarked white or dark van the size of a Sprinter "
    "still counts. Reply YES if you see at least one such vehicle. "
    "Reply NO only if the only vehicles visible are clearly passenger "
    "cars, SUVs, sedans, pickup trucks, or motorcycles. Reply YES or NO only."
)


def fresh_prompt():
    """Defeat llama.cpp's prompt cache by appending a unique nonce to the
    prompt on every call. This forces a cache miss -> full inference path
    runs (vision encoder + prefill + decode), which is the whole point of
    the canary as a smoke test of model state. A cached canary would only
    test the decode path and miss vision-encoder failures.
    """
    return PROMPT_BASE + f" (smoke test {int(time.time()*1000)})"

def pick_cases():
    """Pick one random yes_*.jpg and one random no_*.jpg from the rotation
    set. New choice every canary run -> cache miss -> full inference path."""
    yes_pool = sorted(FRAMES_DIR.glob("yes_*.jpg"))
    no_pool = sorted(FRAMES_DIR.glob("no_*.jpg"))
    if not yes_pool or not no_pool:
        print(f"ERROR: missing rotation set in {FRAMES_DIR}", file=sys.stderr)
        sys.exit(2)
    return [
        (random.choice(yes_pool), "YES"),
        (random.choice(no_pool), "NO"),
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
                {"type": "text", "text": fresh_prompt()},
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
    cases = pick_cases()
    failures = []
    for path, expected in cases:
        name = path.name
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
        print(f"\nCANARY FAIL: {len(failures)} of {len(cases)} cases failed",
              file=sys.stderr)
        sys.exit(1)
    print(f"\nCANARY OK: {len(cases)}/{len(cases)} pass")


if __name__ == "__main__":
    main()
