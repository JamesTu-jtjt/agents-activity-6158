# Gemini 3.1 Flash Lite Run Report

## Results

This report covers the `gemini-3.1-flash-lite` trajectory recorded in
`logs/run-20260923-172847.jsonl`. The agent used 20 successful model turns and
stopped after detecting the same action three times. The retained best revision
builds successfully, passes the semver.org precedence chain, and has no quality
rule violations.

| **Compile Rate** | **Test Pass Rate** | **#Unsafe blocks** |
| ---------------- | ------------------ | ------------------ |
| **100%** | **97.6% (2367/2425)** | **0** |

The table reports the final artifact's release-build result and the seed-0
differential test rate. Across the five validation seeds, correctness ranged
from 97.57% to 98.10%:

| Category | Passed | Total | Rate |
| --- | ---: | ---: | ---: |
| Valid parsing | 318 | 318 | 100.0% |
| Invalid rejection | 113 | 171 | 66.1% |
| Comparison | 664 | 664 | 100.0% |
| Bumping | 954 | 954 | 100.0% |
| Round-trip formatting | 318 | 318 | 100.0% |
| **Overall** | **2367** | **2425** | **97.6%** |

The remaining 58 seed-0 failures are all invalid strings that the parser accepts.
Examples include leading zeros in core numbers, empty prerelease or build
suffixes, empty dot-separated identifiers, numeric prerelease identifiers with
leading zeros, and characters outside the permitted ASCII set. `cargo test`
succeeds, but the generated library contains no authored Rust unit tests, so
Cargo collected 0 tests. This is a limitation distinct from the differential
pass rate reported above.

## Agent contribution versus scaffold

My estimate is **65% agent and 35% scaffold**. The model inspected the Python
source, produced the Rust design, implemented parsing, formatting, comparison,
and bump operations, and reached full correctness in four of five differential
categories. The scaffold supplied substantial leverage: a specification-rich
system prompt, phase-scoped tools, automatic compilation and evaluation across
five seeds, quality checks, adversarial probes, best-revision rollback, bounded
context, and repetition-based termination. Without the model there is no
translation; without the scaffold, later regressions would have overwritten the
97.6% candidate and the run would not have retained a usable result.

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
precedence, formatting, bump behavior, build-metadata handling, and valid parsing
correctly, but did not finish the rejection grammar. Deterministic evaluation
made that gap visible, while rollback protected the best compiling revision from
the model's later regressions. A targeted follow-up should strengthen only the
parser's lexical validation and add Rust unit tests for the rejected edge cases.
