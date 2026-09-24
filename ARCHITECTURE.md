# Agent Architecture

## At a glance

The agent is a single-model translation loop wrapped in a deterministic safety
harness. Gemini inspects bounded slices of the Python reference and proposes
Rust edits; the harness builds, tests, evaluates, and quality-checks every edit,
keeps only the best validated revision, and rolls back regressions. A compact
state ledger, explicit stop conditions, a 40-call ceiling, and JSONL event logs
keep the process bounded and auditable.

The agent uses a stochastic model for source-grounded code edits and a
deterministic harness for every acceptance decision. Predictable operations are
implemented in code rather than delegated to the model.

```mermaid
flowchart TD
    A["Start from Rust stub"] --> B["Load .env<br/>Gemini key and model"]
    B --> C["Initialize state ledger<br/>40-call hard cap"]

    C --> D{"Harness phase"}

    D -- Grounding --> E["Gemini receives compact context<br/>and only 4 relevant tools"]
    E --> F{"Selected action"}

    F -- Inspect --> G["Read/search Python source and tests<br/>or inspect current Rust"]
    G --> H["Compress result into state ledger"]
    H --> D

    F -- Edit --> I["Write or replace lib.rs"]
    I --> J["Automatic deterministic transaction"]

    subgraph Gate["Non-optional acceptance gate"]
        J --> K["cargo build --release"]
        K -- Fail --> R["Restore best validated revision"]
        K -- Pass --> L["cargo test --release"]
        L -- Fail --> R
        L -- Pass --> M["evaluate.py<br/>5 seeds x n=300"]
        M --> N["23-case adversarial holdout<br/>large identifiers, ASCII, metadata, boundaries"]
        N --> O["Quality scan<br/>unsafe, panic, todo, dependencies, cloning"]
        O --> P["Rank candidate<br/>compliance -> tests -> holdout -> score -> quality"]
    end

    P -- Better --> Q["Promote candidate as new best"]
    P -- Regression --> R
    Q --> S{"All success criteria met?"}
    R --> T["Diagnosis phase<br/>compiler/test/failure evidence"]
    T --> E

    S -- No --> T
    S -- Yes --> U["Validated completion<br/>100% all seeds + holdout + clean quality"]

    U --> V["Full final evaluate.py"]
    V --> W["JSONL trajectory and report"]
    W --> X["Human review before leaderboard submission"]

    C -. every event .-> Y["Audit log<br/>model, tools, scores, phases, rollback, usage"]
    E -. selected repository context .-> Z["Google Gemini API"]
```

## Design decisions

- **Deterministic acceptance:** Every Rust edit automatically triggers a release
  build, Rust tests, differential evaluation, adversarial checks, quality checks,
  and best-revision handling. The model cannot skip these steps.
- **Centralized orchestration:** The translation is sequential and tightly
  coupled, so one agent is used instead of adding multi-agent coordination risk.
- **Phase-scoped tools:** The grounding phase exposes only inspection and initial
  implementation tools. Diagnostic tools become available after an edit.
- **Bounded context:** Each call receives a state ledger and compact recent-action
  digest rather than the complete trajectory or repeated full-file versions.
- **Independent validation:** Five deterministic seeds at `n=300` are combined
  with a 23-case adversarial matrix covering identifier length, ASCII-only syntax,
  metadata precedence, and numeric boundaries.
- **Rule-aware ranking:** Rule compliance, passing tests, adversarial validation,
  behavioral correctness, test count, and avoidable ownership debt determine
  whether a candidate becomes the new best revision.
- **Recoverable failure:** Build failures, test failures, and evaluation
  regressions restore the best validated library instead of corrupting later work.
- **Explicit termination:** Success requires 100% correctness on all local seeds,
  a passing precedence chain and adversarial matrix, passing Rust tests, and zero
  assignment violations. Plateau, repetition, empty turns, and budget exhaustion
  are separate stop conditions.
- **Auditable execution:** The JSONL trajectory records model metadata, token
  usage, active tools, actions, outputs, scores, state, rollback, and stop reason.

Repository source and diagnostics cross the external boundary to Gemini only
during an explicitly authorized agent run. Leaderboard submission remains a
separate human-approved action.
