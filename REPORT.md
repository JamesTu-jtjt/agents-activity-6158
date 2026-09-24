# Gemini 3.5 Flash Lite Run Report

## Results

This report covers the `gemini-3.5-flash-lite` trajectory recorded in
`logs/run-20260923-224018.jsonl`. The experiment began from the repository's
original Rust stub. Gemini received a compact verified grounding packet and
produced the complete implementation in one model call and one `write_rust`
action. The deterministic harness accepted that first candidate without manual
code cleanup.

| **Compile Rate** | **Test Pass Rate** | **#Unsafe blocks** |
| ---------------- | ------------------ | ------------------ |
| **100%** | **100% (2425/2425)** | **0** |

All five fixed validation seeds scored 100%. The final artifact also passes four
authored Rust tests, the SemVer precedence chain, and the 23-case adversarial
holdout covering strict ASCII syntax, metadata rules, large identifiers, and
numeric boundaries.

| Category | Passed | Total | Rate |
| --- | ---: | ---: | ---: |
| Valid parsing | 318 | 318 | 100.0% |
| Invalid rejection | 171 | 171 | 100.0% |
| Comparison | 664 | 664 | 100.0% |
| Bumping | 954 | 954 | 100.0% |
| Round-trip formatting | 318 | 318 | 100.0% |
| **Overall** | **2425** | **2425** | **100.0%** |

No differential points were lost. The evaluator reports four `.unwrap()` calls,
all confined to generated unit tests. They are below the evaluator's suspicious
threshold and are not assignment violations, but they are the main remaining
quality debt.

## Agent contribution versus scaffold

My estimate is **70% agent and 30% scaffold**. The model authored the complete
Rust parser, formatter, comparison logic, bump functions, and tests in a single
turn. The scaffold made that turn effective by supplying the fixed API and a
source-grounded SemVer summary, limiting the first action to a coherent full-file
write, and then independently deciding acceptance. Compilation, Rust tests, five
differential seeds, the adversarial holdout, quality checks, rollback, explicit
termination, and the 20-call experiment budget were deterministic code rather
than model judgment.

## Unintended behavior

The generated library passed every behavioral gate immediately, so there were no
regressive edits or repeated actions. The only unintended behavior was using
`.unwrap()` in four test assertions despite the prompt asking the model to avoid
it. Because these calls are test-only and all production paths remain panic-free,
the harness correctly retained the implementation while reporting the debt.

## Challenges and mitigations

Earlier trajectories spent scarce free-tier calls repeatedly inspecting source
before attempting an implementation, and Gemini intermittently returned HTTP
503 or exhausted model-specific daily quotas. The client now applies bounded
exponential backoff with jitter, honors provider delays, spaces requests for the
free tier, and fails fast on a per-day quota error.

For this experiment, the pipeline front-loaded a compact authoritative packet:
the exact Rust stub, strict parser grammar, prerelease comparison rules,
metadata behavior, bump semantics, and test expectations. Only `write_rust` was
available on the first turn; source and diagnostic tools would reopen after that
attempt if validation failed. This removed low-value browsing without weakening
robustness because every deterministic acceptance and recovery gate remained in
place.
