#!/usr/bin/env python3
"""Eval harness for delivery-truck-detector classification quality.

Runs each labeled case in dataset.json N times under a configurable
(model, prompts, image-resize, jpeg-quality) combination, then reports
per-stage and end-to-end accuracy. Saves a JSON result file for later
comparison.

Usage:
    OPENROUTER_API_KEY=... python run_eval.py
    OPENROUTER_API_KEY=... python run_eval.py --resize 800x450
    OPENROUTER_API_KEY=... python run_eval.py --model google/gemini-2.5-flash
    OPENROUTER_API_KEY=... python run_eval.py --trials 3 --output results/experiment.json
    OPENROUTER_API_KEY=... python run_eval.py --vs results/baseline.json

The default config matches what the Pi runs in production -- run it
with no flags to produce/refresh the production baseline. Then change
one knob at a time and compare.
"""

import argparse
import base64
import io
import json
import os
import pathlib
import sys
import time
from collections import Counter

import requests
from PIL import Image

HERE = pathlib.Path(__file__).parent

# ---------------------------------------------------------------------------
# Production prompts -- mirror what /pi/detector.py is currently running.
# Keep these in sync with detector.py when the prompts change there.
# ---------------------------------------------------------------------------
BINARY_PROMPT_PROD = (
    "Is there a UPS, FedEx, or Amazon delivery vehicle clearly "
    "visible in this image? ONLY say YES if you can clearly see one of "
    "these carriers' branding (UPS shield, FedEx wordmark, or Amazon "
    "smile/Prime logo). Say NO for USPS trucks, unmarked vans, passenger "
    "cars, SUVs, and anything else. Reply YES or NO only."
)

CARRIER_PROMPT_PROD = (
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

ALL_VERDICTS = {"UPS", "FEDEX", "AMAZON", "USPS", "OTHER", "NONE"}
TRACKED_CARRIERS = {"UPS", "FEDEX", "AMAZON"}
PREMIUM_CARRIERS = {"UPS", "FEDEX"}


# ---------------------------------------------------------------------------
# Image preprocessing -- by default, send the raw bytes (matches production,
# which sends whatever ffmpeg writes). With --resize we shrink first to test
# whether smaller images still give correct answers.
# ---------------------------------------------------------------------------
def encode_image(path, resize_max=None, quality=75):
    if resize_max is None:
        return base64.b64encode(pathlib.Path(path).read_bytes()).decode()
    img = Image.open(path).convert("RGB")
    w, h = resize_max
    img.thumbnail((w, h))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Gemini call -- returns (raw_response, prompt_tokens, completion_tokens).
# Token counts come from the API response when available; fall back to None.
# ---------------------------------------------------------------------------
def call_gemini(api_key, model, image_b64, prompt, max_tokens=20, retries=4):
    last_err = None
    for attempt in range(retries):
        try:
            with requests.Session() as s:
                resp = s.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                                {"type": "text", "text": prompt},
                            ],
                        }],
                    },
                    timeout=(15, 90),
                )
                resp.raise_for_status()
                body = resp.json()
                content = (body["choices"][0]["message"].get("content") or "").strip().upper()
                usage = body.get("usage") or {}
                return content, usage.get("prompt_tokens"), usage.get("completion_tokens")
        except requests.exceptions.RequestException as e:
            last_err = e
            backoff = 1.5 ** attempt
            print(f"      retry {attempt+1}/{retries} after {backoff:.1f}s: {type(e).__name__}", file=sys.stderr)
            time.sleep(backoff)
    raise last_err


def parse_binary(raw):
    return "YES" if "YES" in raw else "NO"


def parse_carrier(raw):
    return next((v for v in ALL_VERDICTS if v in raw), "NONE")


# ---------------------------------------------------------------------------
# Per-case runner -- runs Stage 1 N times on the tentative frame and Stage 2
# N times on the confirm frame. Aggregates by majority vote.
# ---------------------------------------------------------------------------
def run_case(api_key, case, config):
    base = HERE
    tent = encode_image(base / case["tentative_frame"],
                        resize_max=config["resize_max"],
                        quality=config["jpeg_quality"])
    conf = encode_image(base / case["confirm_frame"],
                        resize_max=config["resize_max"],
                        quality=config["jpeg_quality"])

    stage1_results = []
    stage2_results = []
    tokens_in = 0
    tokens_out = 0

    for _ in range(config["trials"]):
        raw, p, c = call_gemini(api_key, config["binary_model"], tent,
                                config["binary_prompt"], config["max_tokens"])
        stage1_results.append(parse_binary(raw))
        if p: tokens_in += p
        if c: tokens_out += c

        raw, p, c = call_gemini(api_key, config["carrier_model"], conf,
                                config["carrier_prompt"], config["max_tokens"])
        stage2_results.append(parse_carrier(raw))
        if p: tokens_in += p
        if c: tokens_out += c

    # Majority vote (ties broken arbitrarily by Counter).
    stage1_majority = Counter(stage1_results).most_common(1)[0][0]
    stage2_majority = Counter(stage2_results).most_common(1)[0][0]

    # End-to-end outcome under the production routing logic.
    if stage1_majority == "YES" and stage2_majority in TRACKED_CARRIERS:
        actual_fire = True
        actual_tier = "premium" if stage2_majority in PREMIUM_CARRIERS else "all"
    else:
        actual_fire = False
        actual_tier = None

    expected_fire = case["should_fire"]
    expected_tier = case["expected_tier"]
    correct = (actual_fire == expected_fire) and (actual_tier == expected_tier)

    return {
        "id": case["id"],
        "category": case["category"],
        "stage1_results": stage1_results,
        "stage2_results": stage2_results,
        "stage1_majority": stage1_majority,
        "stage2_majority": stage2_majority,
        "actual_fire": actual_fire,
        "actual_tier": actual_tier,
        "expected_fire": expected_fire,
        "expected_tier": expected_tier,
        "correct": correct,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(run):
    cfg = run["config"]
    results = run["results"]
    correct = sum(1 for r in results if r["correct"])
    total = len(results)

    print()
    print("=" * 90)
    print(f"Eval: {cfg['label']}")
    print(f"  binary={cfg['binary_model']} carrier={cfg['carrier_model']}")
    print(f"  resize={cfg['resize_max']} quality={cfg['jpeg_quality']} trials={cfg['trials']}")
    print("=" * 90)

    by_cat = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    for cat, rs in sorted(by_cat.items()):
        cat_correct = sum(1 for r in rs if r["correct"])
        print(f"\n  [{cat}]  ({cat_correct}/{len(rs)})")
        for r in rs:
            mark = "OK  " if r["correct"] else "MISS"
            s1_dist = dict(Counter(r["stage1_results"]))
            s2_dist = dict(Counter(r["stage2_results"]))
            actual = f"fire={r['actual_fire']}/tier={r['actual_tier']}"
            expected = f"fire={r['expected_fire']}/tier={r['expected_tier']}"
            print(f"    {mark}  {r['id']:34s}")
            print(f"          stage1={s1_dist}  stage2={s2_dist}")
            print(f"          got: {actual}    expected: {expected}")

    print()
    print(f"  Total: {correct}/{total} correct ({100*correct/total:.1f}%)")

    # Cost: we don't have authoritative pricing without scraping OpenRouter.
    # Just report token totals. User can apply $/MT for the model in question.
    total_in = sum(r["tokens_in"] for r in results)
    total_out = sum(r["tokens_out"] for r in results)
    print(f"  Tokens: {total_in:,} in, {total_out:,} out (across {total} cases x {cfg['trials']} trials x 2 stages)")


def compare_runs(baseline, current):
    """Diff two saved runs by case ID."""
    base_by_id = {r["id"]: r for r in baseline["results"]}
    curr_by_id = {r["id"]: r for r in current["results"]}

    print()
    print("=" * 90)
    print(f"Compare:  baseline={baseline['config']['label']}  vs  current={current['config']['label']}")
    print("=" * 90)

    regressions = []
    fixes = []
    unchanged_correct = 0
    unchanged_wrong = 0

    all_ids = sorted(set(base_by_id) | set(curr_by_id))
    for cid in all_ids:
        b = base_by_id.get(cid)
        c = curr_by_id.get(cid)
        if b is None or c is None:
            print(f"  -- {cid}: present in only one run, skipping")
            continue
        if b["correct"] and c["correct"]:
            unchanged_correct += 1
        elif (not b["correct"]) and (not c["correct"]):
            unchanged_wrong += 1
        elif b["correct"] and not c["correct"]:
            regressions.append((cid, b, c))
        else:
            fixes.append((cid, b, c))

    print(f"\n  Unchanged (still correct):  {unchanged_correct}")
    print(f"  Unchanged (still wrong):    {unchanged_wrong}")
    print(f"  Fixed (wrong -> correct):   {len(fixes)}")
    print(f"  Regressed (correct -> wrong): {len(regressions)}")

    if regressions:
        print("\n  REGRESSIONS:")
        for cid, b, c in regressions:
            print(f"    {cid}")
            print(f"      baseline: stage1={Counter(b['stage1_results'])}  stage2={Counter(b['stage2_results'])}  -> fire={b['actual_fire']}")
            print(f"      current:  stage1={Counter(c['stage1_results'])}  stage2={Counter(c['stage2_results'])}  -> fire={c['actual_fire']}")
    if fixes:
        print("\n  FIXES:")
        for cid, b, c in fixes:
            print(f"    {cid}")
            print(f"      baseline: stage1={Counter(b['stage1_results'])}  stage2={Counter(b['stage2_results'])}  -> fire={b['actual_fire']}")
            print(f"      current:  stage1={Counter(c['stage1_results'])}  stage2={Counter(c['stage2_results'])}  -> fire={c['actual_fire']}")

    base_tokens = sum(r["tokens_in"] + r["tokens_out"] for r in baseline["results"])
    curr_tokens = sum(r["tokens_in"] + r["tokens_out"] for r in current["results"])
    if base_tokens and curr_tokens:
        delta = (curr_tokens - base_tokens) / base_tokens * 100
        print(f"\n  Tokens: baseline={base_tokens:,}, current={curr_tokens:,} ({delta:+.1f}%)")


# ---------------------------------------------------------------------------
# Config + CLI
# ---------------------------------------------------------------------------
def parse_resize(s):
    if s is None:
        return None
    w, h = s.lower().split("x")
    return (int(w), int(h))


def build_config(args):
    binary_prompt = BINARY_PROMPT_PROD
    carrier_prompt = CARRIER_PROMPT_PROD
    if args.binary_prompt_file:
        binary_prompt = pathlib.Path(args.binary_prompt_file).read_text().strip()
    if args.carrier_prompt_file:
        carrier_prompt = pathlib.Path(args.carrier_prompt_file).read_text().strip()

    label_parts = [args.label] if args.label else []
    if not label_parts:
        label_parts.append(f"model={args.model}")
        if args.resize:
            label_parts.append(f"resize={args.resize}")
        if args.jpeg_quality != 75:
            label_parts.append(f"q={args.jpeg_quality}")

    return {
        "label": " ".join(label_parts) if label_parts else "default",
        "binary_model": args.binary_model or args.model,
        "carrier_model": args.carrier_model or args.model,
        "binary_prompt": binary_prompt,
        "carrier_prompt": carrier_prompt,
        "resize_max": parse_resize(args.resize),
        "jpeg_quality": args.jpeg_quality,
        "trials": args.trials,
        "max_tokens": args.max_tokens,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="google/gemini-3-flash-preview",
                   help="Model ID for both stages unless overridden")
    p.add_argument("--binary-model", default=None, help="Override model for Stage 1 only")
    p.add_argument("--carrier-model", default=None, help="Override model for Stage 2 only")
    p.add_argument("--resize", default=None,
                   help="WxH to thumbnail-fit images before sending (e.g. 800x450). "
                        "Default: send raw bytes (matches production).")
    p.add_argument("--jpeg-quality", type=int, default=75,
                   help="JPEG quality on resized output (only used if --resize is set)")
    p.add_argument("--trials", type=int, default=5,
                   help="Trials per case per stage")
    p.add_argument("--max-tokens", type=int, default=20)
    p.add_argument("--binary-prompt-file", default=None,
                   help="Override binary prompt with contents of file")
    p.add_argument("--carrier-prompt-file", default=None,
                   help="Override carrier prompt with contents of file")
    p.add_argument("--label", default=None, help="Free-form label to identify this run")
    p.add_argument("--output", default=None, help="Save full results to this JSON path")
    p.add_argument("--vs", default=None,
                   help="Compare current run against a previously saved baseline JSON")
    p.add_argument("--dataset", default=str(HERE / "dataset.json"))
    args = p.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: set OPENROUTER_API_KEY", file=sys.stderr)
        sys.exit(1)

    dataset = json.loads(pathlib.Path(args.dataset).read_text())
    cases = dataset["cases"]
    config = build_config(args)

    print(f"Running {len(cases)} cases x {config['trials']} trials x 2 stages = "
          f"{len(cases) * config['trials'] * 2} API calls")

    results = []
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case['id']}", flush=True)
        try:
            results.append(run_case(api_key, case, config))
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            results.append({
                "id": case["id"],
                "category": case["category"],
                "error": str(e),
                "correct": False,
                "stage1_results": [], "stage2_results": [],
                "stage1_majority": None, "stage2_majority": None,
                "actual_fire": None, "actual_tier": None,
                "expected_fire": case["should_fire"],
                "expected_tier": case["expected_tier"],
                "tokens_in": 0, "tokens_out": 0,
            })

    run = {
        "config": config,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataset_version": dataset.get("version"),
        "n_cases": len(cases),
        "results": results,
    }

    print_report(run)

    if args.output:
        out_path = pathlib.Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(run, indent=2))
        print(f"\nSaved: {out_path}")

    if args.vs:
        baseline = json.loads(pathlib.Path(args.vs).read_text())
        compare_runs(baseline, run)


if __name__ == "__main__":
    main()
