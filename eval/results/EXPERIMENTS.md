# Experiments log

A running log of eval runs, their config, and the verdict. The point is to
not re-litigate a blind alley after we've already walked it. Each row
links to the saved JSON in this directory.

| Date | Label | Config | Accuracy | Verdict |
|---|---|---|---|---|
| 2026-04-30 | `baseline` | gemini-3-flash-preview, no resize, q75, trials=5 | **24/25 (96%)** | Reference. 1 known failure: `apr29_fedex_partial_arrival`. |
| 2026-04-30 | `gemini-2.5-flash-lite` | model swap | 20/25 (80%) | **Don't ship.** Stage 1 too conservative on real catches — missed 3 of 5 important real catches (apr27 Amazon × 2, apr29 FedEx). 4 regressions, 0 fixes. Net cost ~$0.05/run vs $0.15. |
| 2026-04-30 | `gpt-4.1-nano` | model swap | 16/25 (64%) | **Don't ship.** Stage 1 weak + can't classify UPS (4/5 OTHER). One unparseable response. 8 regressions, 0 fixes. |
| 2026-04-30 | `nova-lite` | model swap | 15/25 (60%) | **Don't ship.** Hallucinates UPS everywhere — empty streets, USPS trucks, the Amazon Flex sedan. 10 regressions including new false positives. The one "fix" was wrong-for-the-right-reason (called FedEx UPS). |
| 2026-04-30 | `resize-800x450-q50` | gemini-3-flash + smaller images | 21/25 (84%) | **Don't bother.** Real cost: identical to baseline ($0.150) — Gemini 3 Flash's image tokenizer normalizes every image to ~258 tokens regardless of pixel dimensions. So resize buys nothing on this model, but costs us 3 regressions on Stage 1 (apr27 amazon first, apr29 fedex real, apr30 amazon real). Resize is dead as a cost-cutting strategy here. |
| 2026-04-30 | `gemini-2.5-flash` | model swap (mid-tier Google) | 20/25 (80%) | **Don't ship.** Headline 40% cheaper price ($0.30/M vs $0.50/M) is eaten by +58% token bloat — real cost basically identical ($0.142 vs $0.150). 4 Stage 1 regressions on real catches. |

## Recurring observation: Stage 1 is the bottleneck

Across **every** cheaper-or-shrunk config tested, the same handful of
real-catch cases regress at Stage 1: apr27_amazon_first, apr27_amazon_second,
apr29_fedex_real, apr30_amazon_real. Stage 2 is rarely the problem.
Full-res Gemini 3 Flash is right at the threshold of signal needed to
clear Stage 1 reliably. Any of: a smaller model, a smaller image, or a
different image tokenizer pushes one or more of these below threshold.

The cooldown-paused architecture means Stage 2 only runs ~1× per day in
production, so swapping just Stage 2 saves almost nothing. **There is no
realistic cost-cut that doesn't hurt recall.** Baseline holds.

## Token bloat caveat

The cheaper-per-token models (gemini-2.5-flash-lite, gpt-4.1-nano,
nova-lite, gemini-2.5-flash) all used 58-90% MORE input tokens than
Gemini 3 Flash for the same images. Gemini 3 Flash's image tokenizer is
unusually compact (~258 tokens/image vs 1500-2300 elsewhere). This eats
roughly half of the headline cost advantage. Always measure cost from
the saved JSON's `total_input_tokens`, not the listed $/M-token rate.

## Resize buys nothing on Gemini 3 Flash

Confirmed empirically: 800x450 q50 vs 1920x1080 q75 → both cost ~258
tokens per image. Gemini's image tokenizer is dimension-invariant in this
range. Resize as a cost lever only matters for models that price by
pixel count.
