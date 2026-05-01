# Experiments log

A running log of eval runs, their config, and the verdict. The point is to
not re-litigate a blind alley after we've already walked it. Each row
links to the saved JSON in this directory.

| Date | Label | Config | Accuracy | Verdict |
|---|---|---|---|---|
| 2026-05-01 | `baseline` (rev) | gemini-3-flash-preview, no resize, q75, trials=5 | **23/25 (92%)** | Reference, after relabeling apr29 afternoon (was mislabeled as empty; actually a real Amazon delivery production missed). 2 known failures: `apr29_amazon_missed_afternoon` (production miss — Stage 1 NO 4/5 on a real Amazon van) and `apr29_fedex_partial_arrival`. |
| 2026-04-30 | `baseline` (legacy) | same model, pre-relabel | 24/25 (96%) | Reference at the time, but inflated by mislabeled apr29_empty_afternoon. Not directly comparable to later runs. |
| 2026-04-30 | `gemini-2.5-flash-lite` | model swap | 20/25 (80%) | **Don't ship.** Stage 1 too conservative on real catches — missed 3 of 5 important real catches (apr27 Amazon × 2, apr29 FedEx). 4 regressions, 0 fixes. Net cost ~$0.05/run vs $0.15. |
| 2026-04-30 | `gpt-4.1-nano` | model swap | 16/25 (64%) | **Don't ship.** Stage 1 weak + can't classify UPS (4/5 OTHER). One unparseable response. 8 regressions, 0 fixes. |
| 2026-04-30 | `nova-lite` | model swap | 15/25 (60%) | **Don't ship.** Hallucinates UPS everywhere — empty streets, USPS trucks, the Amazon Flex sedan. 10 regressions including new false positives. The one "fix" was wrong-for-the-right-reason (called FedEx UPS). |
| 2026-04-30 | `resize-800x450-q50` | gemini-3-flash + smaller images | 21/25 (84%) | **Don't bother.** Real cost: identical to baseline ($0.150) — Gemini 3 Flash's image tokenizer normalizes every image to ~258 tokens regardless of pixel dimensions. So resize buys nothing on this model, but costs us 3 regressions on Stage 1 (apr27 amazon first, apr29 fedex real, apr30 amazon real). Resize is dead as a cost-cutting strategy here. |
| 2026-04-30 | `gemini-2.5-flash` | model swap (mid-tier Google) | 20/25 (80%) | **Don't ship.** Headline 40% cheaper price ($0.30/M vs $0.50/M) is eaten by +58% token bloat — real cost basically identical ($0.142 vs $0.150). 4 Stage 1 regressions on real catches. |
| 2026-05-01 | `qwen3-vl-8b-instruct` | model swap (Qwen open-weights, candidate for Jetson local deploy) | 19/25 (76%) | **Don't ship as-is.** Real cost $0.020/run (7.5× cheaper). Same Stage 1 whiff pattern: apr27 amazon × 2, apr29 fedex, apr30 amazon, apr30 ups. **But interesting**: on apr30_ups_real, stage1=NO 5/5 yet stage2=UPS 5/5 — the model CAN see UPS, the binary prompt just isn't clicking. Suggests prompt rephrasing experiment is the next step before writing this off. |
| 2026-05-01 | `qwen3-vl-8b-thinking` | model swap (Qwen CoT variant) | 18/25 (72%) | **Don't ship.** CoT eats 64k output tokens, real cost basically identical to baseline ($0.116). Worse than the instruct variant, plus a new false positive (apr29_fedex_driveby — thinks it's FedEx not USPS). |
| 2026-05-01 | `permissive-binary-gemini` | gemini-3-flash + permissive Stage 1 prompt | 22/25 (pre-relabel 88%) | **Important diagnostic, not a ship.** Relaxing Stage 1 exposes Stage 2 hallucinations on noise/empty scenes. Restrictive Stage 1 was load-bearing for false-positive prevention. |
| 2026-05-01 | `permissive-binary-qwen` | qwen3-vl-8b-instruct + permissive Stage 1 prompt | 19/25 (pre-relabel 76%) | Stage 1 NOW fires correctly on real catches (the wall is breached). New wall: Qwen Stage 2 returns OTHER on Amazon and FedEx (can't distinguish from generic). Pointed at the mix-and-match architecture. |
| 2026-05-01 | `permissive-binary-mandarin-qwen` | same as above, prompt in Mandarin | 19/25 (76%) | **Mandarin made zero difference.** Identical regression list. Failure is at the visual recognition level, not language understanding. Hypothesis disproven. |
| 2026-05-01 | `permissive-binary-gemini-lite` | gemini-2.5-flash-lite + permissive prompt | 19/25 (76%) | Same Stage 2 OTHER pattern as Qwen. Cheap Google model is not better at carrier classification than Qwen. |
| 2026-05-01 | **`qwen-binary-gemini-carrier`** | **Qwen3-VL-8B Stage 1 (permissive) + Gemini 3 Flash Stage 2 (carrier)** | **24/25 (96%)** | **WINNER.** Beats new baseline by +1 case. Catches the Apr 29 Amazon delivery production silently missed. Only remaining failure is apr29_fedex_partial_arrival (baseline misses too). Production cost projected at ~$1.50–4.50/mo (vs ~$15/mo). Requires Jetson local deployment of Qwen. |

## Updated framing (2026-05-01): the right cost-cut is stage specialization

Earlier model-swap experiments suggested "Stage 1 is the bottleneck." That
framing was incomplete. The truth, after the prompt-rephrase experiments:

- **Stage 1 (binary detection)** is *easy* for cheap models — Qwen with a
  permissive prompt fires correctly on every real catch. The "wall" was
  the prompt, not the model.
- **Stage 2 (carrier classification)** is *hard* — cheap models can detect
  "delivery van shape" but cannot reliably distinguish Amazon vans from
  generic vans, FedEx from generic, etc. They return OTHER 5/5 on
  carriers Gemini gets right.
- **Stage 1's restrictiveness is load-bearing** for false-positive
  prevention. Gemini Stage 2 still hallucinates on certain noise/empty
  scenes; the restrictive Stage 1 prompt is what masks those.

This points at a stage-specialized architecture: cheap permissive Stage 1
(local on Jetson, free), Gemini for the precise carrier classification.
Validated empirically — see `qwen-binary-gemini-carrier` row above.

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

## Open thread: prompt rephrasing for Qwen3-VL-8B

The qwen3-vl-8b-instruct run revealed something the other failed swaps
didn't: on apr30_ups_real, stage1=NO 5/5 but stage2=UPS 5/5. The model
sees the UPS truck and labels it correctly when asked "what carrier is
this?" — it just doesn't fire on the current Stage 1 phrasing ("is this
a tracked-carrier delivery truck?"). A more permissive Stage 1 prompt
("is there ANY delivery vehicle visible?") might clear the gate while
keeping Stage 2 to do carrier filtering. Worth a follow-up run before
writing off Qwen for Jetson local deployment — local inference at zero
marginal cost would be transformative if recall holds.
