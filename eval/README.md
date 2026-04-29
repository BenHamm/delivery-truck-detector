# Evaluation Suite

Offline accuracy harness for the two-stage detector. Lets us measure the impact of any change (model swap, image resize, prompt rewrite, JPEG quality drop) against a fixed labeled dataset before deploying to the Pi.

## Layout

```
eval/
├── dataset.json          # Labeled cases: tentative+confirm frame pairs with ground truth
├── frames/               # The actual image files referenced by dataset.json
│   ├── apr24/            # First production catches
│   ├── apr26/            # Noise / hallucination scenes
│   ├── apr27/            # Amazon false-negative + USPS scenes
│   ├── apr29/            # Drive-by FedEx + empty-street references
│   └── synthetic/        # Gemini-generated edge cases
├── results/              # Saved JSON outputs per run, for diffing later
│   └── baseline.json     # Production-config result (reference)
├── run_eval.py           # The harness
└── ingest_from_pi.py     # Helper to grow the dataset from live Pi captures
```

## Running

Set your OpenRouter key, then:

```bash
# Reproduce the production baseline
OPENROUTER_API_KEY=sk-or-v1-... python run_eval.py --label "baseline" --output results/baseline.json

# Try resizing images to ~half resolution
python run_eval.py --resize 800x450 --label "resize-800x450" \
    --output results/resize-800x450.json --vs results/baseline.json

# Try a cheaper model
python run_eval.py --model google/gemini-2.5-flash --label "gemini-2.5-flash" \
    --output results/gemini-2.5-flash.json --vs results/baseline.json

# Try a stricter binary prompt
python run_eval.py --binary-prompt-file experiments/strict-binary.txt \
    --label "strict-binary" --output results/strict-binary.json \
    --vs results/baseline.json
```

Each run prints a per-case + aggregate report and (with `--vs`) a side-by-side diff highlighting regressions and fixes.

## Configuration knobs

| Flag | What it changes | Default |
|---|---|---|
| `--model` | Both stages | `google/gemini-3-flash-preview` |
| `--binary-model` / `--carrier-model` | Just one stage (lets you mix) | inherits from `--model` |
| `--resize WxH` | Thumbnail-fit images before sending. Lower = cheaper but may degrade recall. | none (raw bytes, matches production) |
| `--jpeg-quality N` | JPEG quality after resize | 75 |
| `--trials N` | Trials per case per stage. More trials = tighter signal but linearly more spend. | 5 |
| `--binary-prompt-file` / `--carrier-prompt-file` | Swap prompts | production prompts |
| `--label` | Free-form name for the run | derived from flags |
| `--output` | Save full results JSON | not saved |
| `--vs` | Compare against a previously saved run | none |

## Dataset

`dataset.json` is a versioned list of cases. Each case carries a tentative frame and a confirm frame (paths relative to `eval/`), plus ground-truth labels:

```jsonc
{
  "id": "apr27_amazon_first",
  "category": "real_delivery",
  "description": "Real Amazon delivery, Apr 27 11:50am.",
  "tentative_frame": "frames/apr27/amazon_first_tentative.jpg",
  "confirm_frame":   "frames/apr27/amazon_first_confirm.jpg",
  "expected_stage1_yes": true,    // binary should fire YES on tentative
  "expected_carrier":   "AMAZON", // carrier classifier verdict on confirm
  "should_fire": true,            // end-to-end: should the system notify?
  "expected_tier": "all"          // and to which tier?
}
```

Categories so far:
- **real_delivery** — confirmed UPS/FedEx/Amazon at the building. Should fire.
- **noise** — empty/parked-cars scenes that previously hallucinated a carrier. Should skip.
- **drive_by** — tracked-carrier truck visible in tentative but gone in confirm. Should skip.
- **usps** — USPS truck in scene. Should skip (postal carriers have building access).
- **empty** — pure empty street, sanity check. Should skip.
- **synthetic_real** — Gemini-generated photorealistic delivery scenes. Should fire.
- **synthetic_edge** — Gemini-generated tricky edges (logo-less Sprinter, Amazon Flex personal car). Should skip.

The dataset is small today (~16 cases). It's designed to grow organically: every interesting production event becomes a labeled test case.

## Growing the dataset

When something interesting happens in production (a real catch, a near-miss, a confusing scene):

1. **Pull the frames from the Pi**:
   ```bash
   python ingest_from_pi.py --hours 24
   ```
   This stages YES tentatives, all `_confirm` frames, and (with `--include-none`) NONE samples in `frames/_unlabeled/`.

2. **Review the frames** and decide:
   - What's actually in them?
   - What should the system have done?
   - What's the correct ground truth?

3. **Move the frames** out of `_unlabeled/` into a date-named subdirectory with a descriptive filename (e.g. `frames/apr30/amazon_morning_delivery_tentative.jpg`).

4. **Add a labeled entry** to `dataset.json`. Keep the schema consistent.

5. **Re-run the baseline** so future experiments compare against the updated dataset:
   ```bash
   python run_eval.py --label "baseline" --output results/baseline.json
   ```

6. **Commit** the new frames + dataset.json + refreshed baseline.

## What "correct" means

A case is **correct** if both:
- `actual_fire == expected_fire` (system fired iff it should have)
- `actual_tier == expected_tier` (it fired to the right tier when applicable)

We use majority vote over `--trials` runs. So a case where Stage 2 returns AMAZON 3/5 and NONE 2/5 counts as AMAZON for outcome purposes; the per-trial breakdown is preserved in the saved JSON for finer analysis.

## Cost

Each case = `2 × trials` API calls. With the default 5 trials and ~16 cases, that's 160 calls per run. At Gemini 3 Flash pricing (~$0.0002/call for current image sizes), one full run is ~$0.03. Comparing 5 configs is ~$0.15. Cheap.

If a config under test resizes images aggressively, the per-call cost drops further; the report includes total token counts so you can see the cost delta directly.
