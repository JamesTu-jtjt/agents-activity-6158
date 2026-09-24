# Gemini 3.5 Flash Lite Experiment

## Setup

- Model: `gemini-3.5-flash-lite`
- Starting point: the repository's original Rust stub
- Model-call budget: 20
- Acceptance pipeline: release build, Rust tests, five differential seeds,
  SemVer precedence chain, 23-case adversarial holdout, quality scan, and
  best-revision rollback
- Pipeline variation: the first turn received the fixed Rust stub and a compact
  authoritative SemVer packet, with only `write_rust` available. Diagnostic and
  source-inspection tools remained available after the first attempted edit.

## Result

The model completed the task in one model call and one `write_rust` action.

| Metric | Result |
| --- | ---: |
| Release build | Pass |
| Rust tests | 4 passed, 0 failed |
| Differential score, seed 0 | 2425/2425 (100%) |
| Five-seed minimum | 100% |
| SemVer precedence chain | Pass |
| Adversarial holdout | 23/23 |
| Unsafe blocks | 0 |
| Rule violations | 0 |
| Model calls | 1 |

The four `.unwrap()` calls reported by `evaluate.py` occur only in generated
unit tests. They are not assignment violations and do not affect runtime library
behavior, but they are a small quality regression relative to the manually
cleaned submission implementation, which has zero unwrap calls and eight tests.

## Comparison

The earlier Gemini 3.1 Flash Lite trajectory used 20 model turns and retained a
97.6% implementation with no authored Rust tests and an 18/23 adversarial score.
With the front-loaded grounding packet, Gemini 3.5 Flash Lite reached complete
behavioral correctness and holdout coverage in a single turn. This result
supports the pipeline change: deterministic validation and recovery were kept,
while low-value pre-implementation browsing was eliminated.

Trajectory: `logs/run-20260923-224018.jsonl`.
