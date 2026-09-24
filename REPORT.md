# Gemini 3.1 Flash Lite Run Report

## Results

This report covers the `gemini-3.1-flash-lite` trajectory recorded in
`logs/run-20260923-172847.jsonl` and the final submission cleanup. The agent used
20 successful model turns and produced a 97.6% candidate. Targeted cleanup then
completed strict parsing, unbounded numeric-prerelease comparison, and Rust unit
tests. The final revision builds, passes the SemVer precedence chain and the
23-case adversarial holdout, and has no quality violations.

| **Compile Rate** | **Test Pass Rate** | **#Unsafe blocks** |
| ---------------- | ------------------ | ------------------ |
| **100%** | **100% (2425/2425)** | **0** |

The table reports the final artifact's release-build result and seed-0
differential test rate. All five oracle-consistent validation seeds scored
100%, and all eight authored Rust tests passed:

| Category | Passed | Total | Rate |
| --- | ---: | ---: | ---: |
| Valid parsing | 318 | 318 | 100.0% |
| Invalid rejection | 171 | 171 | 100.0% |
| Comparison | 664 | 664 | 100.0% |
| Bumping | 954 | 954 | 100.0% |
| Round-trip formatting | 318 | 318 | 100.0% |
| **Overall** | **2425** | **2425** | **100.0%** |

An additional exploratory seed exposed a bug in `evaluate.py`: its random
generator can place a leading-zero numeric prerelease in the "valid" pool even
though the reference oracle rejects it. Such a case is impossible for any
implementation to satisfy under that checker. The evaluator was left unchanged;
the agent's five fixed validation seeds are oracle-consistent.

## Agent contribution versus scaffold

My estimate is **60% agent and 40% scaffold/cleanup**. The model inspected the
Python source, produced the Rust design, implemented parsing, formatting,
comparison, and bump operations, reaching 97.6% by step 7. The scaffold supplied
substantial leverage: a specification-rich system prompt, phase-scoped tools,
automatic compilation and evaluation across five seeds, quality checks,
adversarial probes, best-revision rollback, bounded context, and repetition-based
termination. Without the model there is no translation; without the scaffold,
later regressions would have overwritten the 97.6% candidate. Final cleanup
contributed the last 2.4 percentage points and eight focused tests, so it is
included in the scaffold share rather than attributed to the model trajectory.

## Unintended behavior

After finding the best revision at step 7, the agent repeatedly rewrote the
library with a `compare` function returning `i8` even though the fixed public API
requires `std::cmp::Ordering`. Those candidates failed compilation and were
rolled back. It eventually submitted the same ineffective edit three times,
triggering the stuck detector at step 20. The agent also did not add the Rust
unit tests requested by the assignment, despite being told to do so.

## Challenges and mitigations

Gemini intermittently returned HTTP 503 under high demand, and the free-tier
project exposed a five-request-per-minute limit. The original client aborted the
whole trajectory on the first transient error. The client now retries 408, 429,
and 5xx responses with bounded exponential backoff and jitter, honors
provider-supplied retry delays, and spaces calls by 13 seconds. The error parser
was also hardened against heterogeneous Gemini error-detail payloads.

The semantic challenge was strict SemVer validation. The agent implemented
precedence, formatting, bump behavior, and build-metadata handling, but did not
finish the rejection grammar and parsed numeric prerelease identifiers as
bounded integers. Deterministic evaluation exposed both gaps, while rollback
protected the best compiling revision from later regressions. Submission cleanup
added ASCII identifier validation, leading-zero rules, length-based comparison
for arbitrarily large numeric identifiers, the exact public error type, and
focused Rust tests.
