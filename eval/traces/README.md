# Day-trace eval artifacts

Tarballs of full production days — every saved frame from
`/home/pi/detections/` for a given day, paired with `trace.jsonl`
mapping each frame to its production verdict (Stage 1 verdict,
Stage 2 verdict if any, fired-notification flag).

## Why these aren't in git

Each day is ~200 MB. Daily ingest produces ~6 GB/month, ~78 GB/year.
That's far too big for vanilla git, and Git LFS free tier (1 GB) would
fill in a week. So tarballs live here on the local Mac (and on the Pi
under `/home/pi/traces/`); they're `.gitignore`d.

If we ever need cross-machine reproducibility — e.g. running a trace
eval on a different host — we'd promote tarballs to S3 / GCS / etc.,
keeping only `trace.jsonl` summaries in repo. Until then, local-only.

## Naming

`YYYY-MM-DD.tar.gz` — one per active day. Capture is via `at`-scheduled
`pi/ingest_day_trace.py` running at 20:01 PT (right after active hours
end at 20:00). Pulls every saved frame and reconstructs verdicts from
the journal.

## Inside each tarball

```
2026-05-02/
    frames/                     ~1,200 jpg files (one per poll)
    trace.jsonl                 line-per-frame: ts, gate verdict, carrier, etc.
    summary.json                aggregate stats for the day
```

## Storage policy (informal)

- Active week: keep all daily tarballs on the Mac
- Older than 7 days: keep only the days that had real catches or
  notable disagreements; archive others off the Mac (or delete)
- The Pi rotates its own copies after 36h via the existing detector
  cleanup logic? *(actually it doesn't — `/home/pi/traces/` has no
  retention; will need a separate cleanup if the SD card fills.)*

## Known issues with these traces as eval ground truth

The `trace.jsonl` records the **production verdict**, which is not
always correct. Notably for the May 2 trace: the 18:44 frame was a
real UPS truck that production misclassified as AMAZON, so the
trace's `gate_verdict` for that frame is misleading. Hand-corrections
to `trace.jsonl` may be needed before using a trace as a strict
regression test.

For carrier-coverage testing, prefer the curated `eval/dataset.json`
(27 hand-labeled cases as of 2026-05-02). Use traces for fire-rate
calibration and routine-traffic parity testing — different roles.
