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

## Recurring observation: Stage 1 is the bottleneck

All three cheaper models above were measurably weaker at Stage 1 on real
delivery catches than Gemini 3 Flash. Stage 1 is the cheap-but-decisive
gate — when it says NO on a real catch, the system misses entirely.
Future experiments that swap *only* Stage 2 are unlikely to hurt accuracy
much, but the savings are also tiny (Stage 2 only runs ~1× per day in
production).

## Token bloat caveat

The cheaper-per-token models (gemini-2.5-flash-lite, gpt-4.1-nano,
nova-lite) all used 60-90% MORE input tokens than Gemini 3 Flash for the
same images. Gemini 3 Flash's image tokenizer is unusually compact
(~258 tokens/image vs 1500-2300 elsewhere). This eats roughly half of
the headline cost advantage. Always measure cost from the saved JSON's
`total_input_tokens`, not the listed $/M-token rate.
