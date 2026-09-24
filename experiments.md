# Model Experiments

This document compares two Gemini Flash Lite runs on the Python-to-Rust SemVer
translation task. Both runs began from the original Rust stub and used the same
deterministic acceptance principles: compile, run Rust tests, compare against
the Python oracle, check the SemVer precedence chain, scan for prohibited code,
and retain the best validated revision.

The comparison is not a controlled model-only benchmark. The Gemini 3.5 Flash
Lite run also used an improved first-turn grounding pipeline, described below.

## Summary

| Metric | Gemini 3.1 Flash Lite | Gemini 3.5 Flash Lite |
| --- | ---: | ---: |
| Successful model turns | 20 | **1** |
| Tool actions | 20 | **1** |
| Final differential score | 97.6% | **100%** |
| Valid parsing | 100% | **100%** |
| Invalid rejection | 66.1% | **100%** |
| Comparison | 100% | **100%** |
| Bumping | 100% | **100%** |
| Round-trip formatting | 100% | **100%** |
| Rust tests | 0 | **4 passed** |
| SemVer precedence chain | Pass | Pass |
| Adversarial holdout | 18/23 | **23/23** |
| Unsafe blocks | 0 | 0 |
| Rule violations | 0 | 0 |
| `.unwrap()` calls | **0** | 4, test-only |
| Stop reason | Repeated action | Validated success |

## Gemini 3.1 Flash Lite

Trajectory: `logs/run-20260923-172847.jsonl`

The first run used the original browse-first pipeline. The model inspected the
Python source, iteratively wrote the Rust file, and found its best candidate at
step 7. That candidate implemented valid parsing, comparison, formatting, and
all bump operations correctly, but its parser accepted malformed versions such
as leading-zero core numbers, empty identifiers, invalid characters, and
numeric prerelease identifiers with leading zeros.

Later turns repeatedly produced a `compare` function with the wrong return type.
The deterministic rollback mechanism protected the 97.6% candidate, and the run
stopped after the same ineffective action was repeated three times. It did not
add Rust unit tests.

## Gemini 3.5 Flash Lite

Trajectory: `logs/run-20260923-224018.jsonl`

The second run used a 20-call experiment budget and a front-loaded grounding
packet. The first prompt included the exact Rust stub plus a compact verified
summary of strict SemVer grammar, precedence, metadata, bump behavior, large
numeric identifiers, and testing expectations. Only `write_rust` was exposed on
the first turn; source-inspection and diagnostic tools would reopen if that edit
failed validation.

Gemini 3.5 Flash Lite produced a complete implementation in its first model turn.
The candidate scored 100% on all five fixed validation seeds, passed four Rust
tests, passed the SemVer precedence chain, and passed all 23 adversarial cases.
The harness therefore stopped immediately with validated success.

The generated tests contain four `.unwrap()` calls. These are not assignment
violations and do not affect production code, but they are the only quality area
where the 3.1 candidate was cleaner.

## Interpretation

The 3.5 Flash Lite experiment was better on every behavioral and efficiency
measure: full correctness, complete holdout coverage, authored tests, and one
model call instead of 20. However, the gain should be attributed to both the
newer model and the pipeline change. Front-loading verified context removed the
low-value source-browsing phase while preserving deterministic validation,
rollback, bounded calls, and source tools for recovery.

The strongest conclusion is architectural: a compact authoritative first-turn
packet can improve both call efficiency and final quality when the task has a
small fixed API and a strong deterministic evaluator. Browse-first recovery is
still valuable, but it does not need to precede the first implementation attempt.
