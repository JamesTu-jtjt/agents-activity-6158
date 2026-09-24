#!/usr/bin/env python3
"""Auditable Python-to-Rust translation agent for the CS 6158 exercise."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).parent
RUST = HERE / "rust"
LIB = RUST / "src" / "lib.rs"
PYSRC = HERE / "reference" / "version.py"
LOGS = HERE / "logs"
STATE_DIR = HERE / ".agent"
BEST_LIB = STATE_DIR / "best-lib.rs"
MAX_MODEL_CALLS = 40
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
_LAST_API_REQUEST_AT: float | None = None


def _load_dotenv(path: pathlib.Path = HERE / ".env") -> None:
    """Load simple KEY=VALUE entries without overriding the process environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value)


_load_dotenv()


@dataclasses.dataclass
class RunState:
    best_score: float | None = None
    best_rank: tuple | None = None
    last_score: float | None = None
    best_step: int | None = None
    last_evaluation: dict | None = None
    evaluations_without_improvement: int = 0
    consecutive_no_tool: int = 0
    repeated_actions: int = 0
    last_action: str | None = None
    model_calls: int = 0
    phase: str = "grounding"
    edit_attempts: int = 0


STATE = RunState()


def _chat_message(message: dict) -> dict:
    """Convert the compact history record to Chat Completions shape."""
    role = message["role"]
    if role != "assistant":
        out = {"role": role, "content": message.get("content", "")}
        if role == "tool":
            out["tool_call_id"] = message["tool_call_id"]
        return out

    out = {"role": "assistant", "content": message.get("content") or None}
    calls = []
    for call in message.get("tool_calls", []):
        wire_call = {
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("arguments") or {}),
            },
        }
        # Gemini 3 requires this encrypted signature to be replayed exactly
        # when returning a function result on the next request.
        if call.get("extra_content") is not None:
            wire_call["extra_content"] = call["extra_content"]
        calls.append(wire_call)
    if calls:
        out["tool_calls"] = calls
    return out


def _provider_config() -> tuple[str, str, str, str]:
    """Return provider, key, model, and an OpenAI-compatible base URL."""
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        return (
            "gemini",
            gemini_key,
            os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
            os.environ.get(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai",
            ).rstrip("/"),
        )
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        return (
            "openai",
            openai_key,
            os.environ.get("OPENAI_MODEL", "gpt-5.6"),
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        )
    raise RuntimeError(
        "no model credential found; set GEMINI_API_KEY (preferred) or OPENAI_API_KEY"
    )


def _retry_after_seconds(exc: urllib.error.HTTPError, detail: str) -> float | None:
    """Extract a provider-supplied retry delay from headers or a JSON error."""
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass

    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    if not isinstance(details, list):
        details = []
    for item in details:
        if not isinstance(item, dict):
            continue
        delay = item.get("retryDelay")
        if isinstance(delay, str):
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", delay)
            if match:
                return float(match.group(1))

    match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", detail, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _pace_api_request() -> None:
    """Keep sequential calls below the configured requests-per-minute ceiling."""
    global _LAST_API_REQUEST_AT
    interval = max(0.0, float(os.environ.get("AGENT_MIN_REQUEST_INTERVAL", "13")))
    if _LAST_API_REQUEST_AT is not None:
        remaining = interval - (time.monotonic() - _LAST_API_REQUEST_AT)
        if remaining > 0:
            time.sleep(remaining)
    _LAST_API_REQUEST_AT = time.monotonic()


def call_model(messages: list[dict], tools: list[dict]) -> dict:
    """Call the configured model, retry transients, and normalize its reply."""
    provider, api_key, model, base_url = _provider_config()
    body = {
        "model": model,
        "messages": [_chat_message(message) for message in messages],
        "tools": [{"type": "function", "function": schema} for schema in tools],
        "tool_choice": "auto",
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = int(os.environ.get("AGENT_TIMEOUT", "180"))
    max_retries = max(0, int(os.environ.get("AGENT_MAX_RETRIES", "6")))
    for attempt in range(max_retries + 1):
        _pace_api_request()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:2000]
            daily_quota = exc.code == 429 and "GenerateRequestsPerDay" in detail
            if (
                exc.code not in RETRYABLE_HTTP_STATUS
                or daily_quota
                or attempt == max_retries
            ):
                raise RuntimeError(f"model API HTTP {exc.code}: {detail}") from exc
            provider_delay = _retry_after_seconds(exc, detail)
            backoff = min(60.0, 2.0**attempt)
            delay = max(backoff, provider_delay or 0.0)
            delay += random.uniform(0.0, min(1.0, delay * 0.2))
            print(
                f"[retry] model API HTTP {exc.code}; retrying in {delay:.1f}s "
                f"({attempt + 1}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == max_retries:
                raise RuntimeError(f"model API request failed: {exc}") from exc
            delay = min(60.0, 2.0**attempt)
            delay += random.uniform(0.0, min(1.0, delay * 0.2))
            print(
                f"[retry] model API request failed; retrying in {delay:.1f}s "
                f"({attempt + 1}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(delay)

    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected model response: {json.dumps(payload)[:2000]}") from exc

    normalized_calls = []
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_invalid_json": call["function"].get("arguments")}
        normalized_calls.append(
            {
                "id": call["id"],
                "name": call["function"]["name"],
                "arguments": arguments,
                "extra_content": call.get("extra_content"),
            }
        )
    return {
        "text": message.get("content"),
        "tool_calls": normalized_calls,
        "provider": provider,
        "model": model,
        "usage": payload.get("usage"),
    }


def system_prompt() -> str:
    return """You are translating reference/version.py into rust/src/lib.rs.
Work autonomously through the provided tools, one deliberate action per turn.
Ground behavior in the Python source and tests: search or read relevant slices
instead of guessing. Never edit rust/src/main.rs or Cargo.toml.

Hard rules: Rust std only; no unsafe, Python callbacks, todo!, unimplemented!,
or panic!. Avoid needless clone(), to_owned(), and unwrap(). Preserve the fixed
public API. Add meaningful Rust unit tests for parsing, invalid inputs,
precedence, build metadata, formatting, and all bumps.

Important known requirements (still verify them in the oracle): strict parsing
requires major.minor.patch; numeric prerelease identifiers reject leading zero,
build identifiers may contain leading zero; build metadata is ignored by
precedence; numeric prerelease identifiers sort below text; shorter equal
prefixes sort first. Numeric prerelease identifiers can be longer than u64, so
compare them without bounded-integer parsing. The assignment's bump functions are the
corresponding Version methods in reference/version.py, not next_version.

Every write_rust or replace_rust action automatically triggers a deterministic
transaction: release build, Rust tests, differential evaluation on five local
seeds, quality checks, and best-revision rollback. You do not need to request
those steps. Use the returned validation evidence to diagnose the next edit.

A task is complete only at 100% differential correctness on every local seed,
a passing precedence chain and cargo tests, and zero quality violations. The
harness—not your confidence—decides completion. Prefer source-grounded edits
over narration, repeated inspection, or speculative rewrites."""


def _compact_tool_content(message: dict) -> str:
    content = message.get("content", "")
    limit = 12000 if message.get("name") in {"read_rust", "write_rust", "replace_rust"} else 3500
    if len(content) <= limit:
        return content
    return content[:limit] + f"\n...[{len(content) - limit} characters omitted]"


def build_context(history: list[dict], step: int) -> list[dict]:
    """Select a bounded ledger and textual action digest.

    Each model call is an independent planning turn. Tool results are carried
    as text rather than as an indefinitely growing provider-native function
    chain. This both bounds context and avoids coupling stored history to a
    provider's opaque function-call state.
    """
    ledger = {
        "step": step,
        "model_calls": STATE.model_calls,
        "best_score": STATE.best_score,
        "best_rank": STATE.best_rank,
        "last_score": STATE.last_score,
        "best_step": STATE.best_step,
        "evaluations_without_improvement": STATE.evaluations_without_improvement,
        "last_evaluation": STATE.last_evaluation,
        "phase": STATE.phase,
        "edit_attempts": STATE.edit_attempts,
    }
    selected = copy.deepcopy(history[2:][-12:])
    digest = []
    for message in selected:
        if message.get("role") == "assistant" and message.get("content"):
            digest.append("MODEL NOTE:\n" + message["content"][:1200])
        for call in message.get("tool_calls", []):
            arguments = call.get("arguments") or {}
            if call.get("name") == "write_rust" and "content" in arguments:
                content = arguments["content"]
                arguments = {
                    "content": (
                        f"<omitted {len(content)} bytes; sha256="
                        f"{hashlib.sha256(content.encode()).hexdigest()[:12]}; "
                        "use read_rust for current code>"
                    )
                }
            elif call.get("name") == "replace_rust":
                arguments = {
                    "old": f"<omitted {len(arguments.get('old', ''))} bytes>",
                    "new": f"<omitted {len(arguments.get('new', ''))} bytes>",
                }
            digest.append(
                f"ACTION {call.get('name')}: " + json.dumps(arguments, sort_keys=True)
            )
        if message.get("role") == "tool":
            digest.append(
                f"RESULT {message.get('name', 'tool')}:\n" + _compact_tool_content(message)
            )
    working_state = (
        "Current run ledger (generated by the harness):\n"
        + json.dumps(ledger)
        + "\n\nRecent action digest:\n"
        + ("\n\n".join(digest) if digest else "(no actions yet)")
        + "\n\nChoose the single best next tool action."
    )
    return history[:2] + [
        {
            "role": "user",
            "content": working_state,
        }
    ]


def should_stop(
    history: list[dict], step: int, budget: int, last_score: float | None
) -> tuple[bool, str]:
    if step >= budget:
        return True, f"budget exhausted ({budget} model calls)"
    result = STATE.last_evaluation or {}
    if (
        last_score == 100.0
        and result.get("cargo_tests_failed") == 0
        and result.get("spec_precedence_chain") is True
        and (result.get("adversarial_holdout") or {}).get("passed") is True
        and not result.get("violations")
    ):
        return True, "validated success: 100% differential, tests pass, clean quality"
    if STATE.evaluations_without_improvement >= 4:
        return True, "plateau: four validated evaluations without improvement"
    if STATE.repeated_actions >= 3:
        return True, "stuck: identical action repeated three times"
    if STATE.consecutive_no_tool >= 2:
        return True, "stuck: two consecutive model turns without a tool action"
    return False, ""


def _run(cmd: list[str], cwd: pathlib.Path | None = None, timeout: int = 180) -> dict:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": (proc.stdout + proc.stderr).strip() or "(no output)",
        }
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")).strip()
        return {
            "ok": False,
            "returncode": None,
            "output": output,
            "error": f"timeout after {timeout}s",
        }


def t_read_source(args: dict) -> str:
    allowed = {"version.py", "test_parsing.py", "test_compare.py", "test_bump.py"}
    filename = args["file"]
    if filename not in allowed:
        return "error: unsupported reference file"
    path = HERE / "reference" / filename
    lines = path.read_text().splitlines()
    start = max(1, int(args["start_line"]))
    end = min(len(lines), int(args["end_line"]))
    if end < start or end - start > 239:
        return "error: request 1-240 ordered lines at a time"
    return "\n".join(f"{number:4}: {lines[number - 1]}" for number in range(start, end + 1))


def t_search_source(args: dict) -> str:
    pattern = args["pattern"]
    if len(pattern) > 120:
        return "error: pattern too long"
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"error: invalid regex: {exc}"
    paths = [PYSRC, *sorted((HERE / "reference").glob("test_*.py"))]
    hits = []
    for path in paths:
        lines = path.read_text().splitlines()
        hits.extend((path.name, lines, index) for index, line in enumerate(lines) if regex.search(line))
    if not hits:
        return "no matches"
    contexts = []
    for filename, lines, index in hits[:20]:
        low, high = max(0, index - 2), min(len(lines), index + 3)
        excerpt = "\n".join(f"{n + 1:4}: {lines[n]}" for n in range(low, high))
        contexts.append(f"[{filename}]\n{excerpt}")
    return "\n---\n".join(contexts)


def t_read_rust(_args: dict) -> str:
    return LIB.read_text()


def t_write_rust(args: dict) -> str:
    content = args["content"]
    if not isinstance(content, str) or not content.strip():
        return "error: content must be a non-empty string"
    LIB.write_text(content)
    return f"wrote {len(content)} bytes to rust/src/lib.rs"


def t_replace_rust(args: dict) -> str:
    old, new = args["old"], args["new"]
    current = LIB.read_text()
    count = current.count(old)
    if count != 1:
        return f"error: old text must occur exactly once; found {count} occurrences"
    LIB.write_text(current.replace(old, new, 1))
    return f"replaced {len(old)} bytes with {len(new)} bytes"


def t_cargo_build(_args: dict) -> str:
    return json.dumps(_run(["cargo", "build", "--release"], cwd=RUST), indent=2)


def t_cargo_test(_args: dict) -> str:
    return json.dumps(_run(["cargo", "test", "--release"], cwd=RUST), indent=2)


def t_probe(args: dict) -> str:
    commands = args["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= 30:
        return "error: commands must contain 1-30 harness protocol strings"
    allowed = ("parse ", "compare ", "bump ", "format ")
    if any(not isinstance(command, str) or not command.startswith(allowed) for command in commands):
        return "error: unsupported harness command"
    build = _run(["cargo", "build", "--release"], cwd=RUST)
    if not build["ok"]:
        return json.dumps({"error": "current library does not build", "build": build}, indent=2)
    binary = RUST / "target" / "release" / "harness"
    try:
        proc = subprocess.run(
            [str(binary)],
            input="\n".join(commands) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "error: probe timed out"
    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "commands": commands,
            "responses": proc.stdout.splitlines(),
            "stderr": proc.stderr,
        },
        indent=2,
    )


def _evaluation_summary(report: dict) -> dict:
    cargo = report.get("cargo_test") or {}
    return {
        "build": report.get("build"),
        "differential_pct": report.get("differential_pct"),
        "differential": report.get("differential"),
        "cargo_tests_passed": cargo.get("passed"),
        "cargo_tests_failed": cargo.get("failed"),
        "spec_precedence_chain": report.get("spec_precedence_chain"),
        "quality": report.get("quality"),
        "violations": report.get("violations", []),
        "error": report.get("error"),
    }


def _evaluation_rank(summary: dict) -> tuple:
    """Rank candidates by rule compliance first, then behavioral strength."""
    quality = summary.get("quality") or {}
    clean = not summary.get("violations")
    tests_clean = summary.get("cargo_tests_failed") == 0
    quality_debt = sum(
        quality.get(name, 0)
        for name in ("clone_calls", "to_owned_calls", "unwrap_calls")
    )
    return (
        int(bool(clean)),
        int(bool(tests_clean)),
        int(bool((summary.get("adversarial_holdout") or {}).get("passed"))),
        int(bool(summary.get("spec_precedence_chain"))),
        float(summary.get("differential_pct") or 0.0),
        int(summary.get("cargo_tests_passed") or 0),
        -quality_debt,
    )


def _adversarial_holdout() -> dict:
    """Check deterministic edge cases absent from the random case generator."""
    import semver

    valid = [
        "18446744073709551615.0.0",
        "1.0.0-999999999999999999999999999999999999999",
        "1.0.0-1000000000000000000000000000000000000000+00001",
        "1.0.0-A.0-a+000.---",
        "0.0.0-0",
    ]
    invalid = [
        "1.0.0-00",
        "1.0.0-000000000000000000000000000000000000001",
        "1.0.0-α",
        "１.0.0",
        "1.0.0+a..b",
    ]
    pairs = [
        (
            "1.0.0-999999999999999999999999999999999999999",
            "1.0.0-1000000000000000000000000000000000000000",
        ),
        ("1.0.0+build.1", "1.0.0+build.999"),
        ("1.0.0-A", "1.0.0-a"),
        ("1.0.0-1", "1.0.0-a"),
        ("1.0.0-a", "1.0.0-a.0"),
    ]
    bumps = [
        ("major", "18446744073709551614.9.9-rc.1+build"),
        ("minor", "7.8.9-rc.1+build"),
        ("patch", "7.8.9-rc.1+build"),
    ]
    commands = [f"parse {value}" for value in valid]
    commands += [f"parse {value}" for value in invalid]
    commands += [f"compare {left} {right}" for left, right in pairs]
    commands += [f"bump {kind} {value}" for kind, value in bumps]
    commands += [f"format {value}" for value in valid]

    binary = RUST / "target" / "release" / "harness"
    try:
        proc = subprocess.run(
            [str(binary)],
            input="\n".join(commands) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "pass": 0, "total": len(commands), "failures": ["timeout"]}
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != len(commands):
        return {
            "passed": False,
            "pass": 0,
            "total": len(commands),
            "failures": [f"harness returned {len(lines)} responses: {proc.stderr[:500]}"],
        }
    got = [json.loads(line) for line in lines]
    expected = []
    for value in valid:
        version = semver.Version.parse(value)
        expected.append(
            {
                "ok": True,
                "major": version.major,
                "minor": version.minor,
                "patch": version.patch,
                "prerelease": version.prerelease,
                "build": version.build,
            }
        )
    expected.extend({"ok": False} for _ in invalid)
    expected.extend(
        {"ok": True, "cmp": semver.Version.parse(left).compare(right)}
        for left, right in pairs
    )
    expected.extend(
        {
            "ok": True,
            "version": str(getattr(semver.Version.parse(value), f"bump_{kind}")()),
        }
        for kind, value in bumps
    )
    expected.extend({"ok": True, "version": value} for value in valid)

    failures = []
    passed = 0
    for command, actual, wanted in zip(commands, got, expected):
        matches = all(actual.get(key) == value for key, value in wanted.items())
        if matches:
            passed += 1
        elif len(failures) < 8:
            failures.append(f"{command}: got {actual}, expected {wanted}")
    return {
        "passed": passed == len(commands),
        "pass": passed,
        "total": len(commands),
        "failures": failures,
    }


def t_evaluate(_args: dict) -> str:
    """Evaluate five local seeds and preserve or restore the best library."""
    STATE_DIR.mkdir(exist_ok=True)
    per_seed = []
    captured_diagnostics = False
    # These seeds produce oracle-valid members in evaluate.py's "valid" pool.
    # Some other seeds expose a generator bug where a numeric prerelease with
    # a leading zero is mislabeled valid even though the oracle rejects it.
    for seed in (0, 17, 271, 9999, 101):
        with tempfile.NamedTemporaryFile(suffix=".json", dir=STATE_DIR, delete=False) as tmp:
            report_path = pathlib.Path(tmp.name)
        try:
            run = _run(
                [
                    sys.executable,
                    str(HERE / "evaluate.py"),
                    "--seed",
                    str(seed),
                    "--n",
                    "300",
                    "--json",
                    str(report_path),
                ],
                cwd=HERE,
                timeout=180,
            )
            if not report_path.exists() or not report_path.read_text().strip():
                return json.dumps({"evaluation_error": run, "seed": seed}, indent=2)
            one = _evaluation_summary(json.loads(report_path.read_text()))
            one["seed"] = seed
            marker = "First failures:"
            if marker in run["output"] and not captured_diagnostics:
                one["first_failures"] = run["output"].split(marker, 1)[1].strip()[:5000]
                captured_diagnostics = True
            per_seed.append(one)
        finally:
            report_path.unlink(missing_ok=True)

    scores = [item.get("differential_pct") for item in per_seed]
    score = min(scores) if all(value is not None for value in scores) else None
    summary = {
        "build": all(item.get("build") for item in per_seed),
        "differential_pct": score,
        "differential_by_seed": {
            str(item["seed"]): item.get("differential_pct") for item in per_seed
        },
        "cargo_tests_passed": per_seed[0].get("cargo_tests_passed"),
        "cargo_tests_failed": max(item.get("cargo_tests_failed") or 0 for item in per_seed),
        "spec_precedence_chain": all(item.get("spec_precedence_chain") for item in per_seed),
        "quality": per_seed[0].get("quality"),
        "violations": sorted(
            {violation for item in per_seed for violation in item.get("violations", [])}
        ),
        "runs": per_seed,
    }
    summary["adversarial_holdout"] = _adversarial_holdout()
    STATE.last_score = score
    STATE.last_evaluation = summary
    rank = _evaluation_rank(summary)
    summary["candidate_rank"] = rank
    improved = score is not None and (STATE.best_rank is None or rank > STATE.best_rank)
    if improved:
        STATE.best_score = score
        STATE.best_rank = rank
        STATE.best_step = STATE.model_calls
        STATE.evaluations_without_improvement = 0
        shutil.copyfile(LIB, BEST_LIB)
        summary["best_revision"] = "updated"
    else:
        STATE.evaluations_without_improvement += 1
        if (
            score is not None
            and STATE.best_score is not None
            and rank < STATE.best_rank
            and BEST_LIB.exists()
        ):
            shutil.copyfile(BEST_LIB, LIB)
            summary["best_revision"] = "restored after regression"
        else:
            summary["best_revision"] = "unchanged"
    summary["best_score"] = STATE.best_score
    summary["best_rank"] = STATE.best_rank
    return json.dumps(summary, indent=2)


def _validate_after_edit() -> str:
    """Deterministically gate a stochastic edit and roll back unsafe states."""
    STATE.phase = "validating"
    STATE.edit_attempts += 1
    build = _run(["cargo", "build", "--release"], cwd=RUST)
    if not build["ok"]:
        restored = _restore_best_if_needed()
        STATE.phase = "diagnose"
        return json.dumps(
            {"stage": "build", "build": build, "restored_best": restored}, indent=2
        )

    tests = _run(["cargo", "test", "--release"], cwd=RUST)
    if not tests["ok"]:
        restored = _restore_best_if_needed()
        STATE.phase = "diagnose"
        return json.dumps(
            {"stage": "tests", "build": build, "tests": tests, "restored_best": restored},
            indent=2,
        )

    evaluation = json.loads(t_evaluate({}))
    successful = (
        evaluation.get("differential_pct") == 100.0
        and evaluation.get("cargo_tests_failed") == 0
        and evaluation.get("spec_precedence_chain") is True
        and (evaluation.get("adversarial_holdout") or {}).get("passed") is True
        and not evaluation.get("violations")
    )
    STATE.phase = "complete" if successful else "diagnose"
    return json.dumps(
        {"stage": "evaluation", "build": build, "tests": tests, "evaluation": evaluation},
        indent=2,
    )


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


TOOLS = [
    dict(
        name="read_source",
        description="Read a numbered slice of the Python source or one relevant test file (at most 240 lines).",
        parameters={
            "type": "object",
            "required": ["file", "start_line", "end_line"],
            "properties": {
                "file": {
                    "type": "string",
                    "enum": ["version.py", "test_parsing.py", "test_compare.py", "test_bump.py"],
                },
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        fn=t_read_source,
    ),
    dict(
        name="search_source",
        description="Regex-search reference/version.py and reference/test_*.py, returning small numbered contexts.",
        parameters={
            "type": "object",
            "required": ["pattern"],
            "properties": {"pattern": {"type": "string"}},
            "additionalProperties": False,
        },
        fn=t_search_source,
    ),
    dict(name="read_rust", description="Read rust/src/lib.rs.", parameters=_empty_schema(), fn=t_read_rust),
    dict(
        name="write_rust",
        description="Overwrite lib.rs, then automatically build, test, evaluate five seeds, and roll back regressions. Use for the initial implementation or a coherent full rewrite.",
        parameters={
            "type": "object",
            "required": ["content"],
            "properties": {"content": {"type": "string"}},
            "additionalProperties": False,
        },
        fn=t_write_rust,
    ),
    dict(
        name="replace_rust",
        description="Replace one exact fragment in lib.rs, then automatically build, test, evaluate five seeds, and roll back regressions.",
        parameters={
            "type": "object",
            "required": ["old", "new"],
            "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
            "additionalProperties": False,
        },
        fn=t_replace_rust,
    ),
    dict(
        name="probe",
        description="Build current code and run 1-30 focused harness commands: parse, compare, bump, or format.",
        parameters={
            "type": "object",
            "required": ["commands"],
            "properties": {"commands": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False,
        },
        fn=t_probe,
    ),
]
BY_NAME = {tool["name"]: tool for tool in TOOLS}
SCHEMAS = [{key: tool[key] for key in ("name", "description", "parameters")} for tool in TOOLS]


def _schemas_for_phase() -> list[dict]:
    """Expose only tools relevant to the current state to reduce routing noise."""
    if STATE.phase == "grounding":
        allowed = {"read_source", "search_source", "read_rust", "write_rust"}
        return [schema for schema in SCHEMAS if schema["name"] in allowed]
    return SCHEMAS


def _restore_best_if_needed() -> bool:
    if BEST_LIB.exists() and STATE.best_score is not None:
        shutil.copyfile(BEST_LIB, LIB)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=MAX_MODEL_CALLS)
    parser.add_argument("--task", default="Translate reference/version.py into rust/src/lib.rs.")
    args = parser.parse_args()
    if not 1 <= args.budget <= MAX_MODEL_CALLS:
        parser.error(f"--budget must be between 1 and {MAX_MODEL_CALLS}")

    LOGS.mkdir(exist_ok=True)
    log = LOGS / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    def rec(**record: object) -> None:
        with log.open("a") as handle:
            handle.write(json.dumps({"t": time.time(), **record}) + "\n")

    history = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": args.task},
    ]
    try:
        provider, _secret, model, _base_url = _provider_config()
    except RuntimeError:
        provider, model = "unconfigured", "unconfigured"
    rec(
        event="start",
        budget=args.budget,
        task=args.task,
        provider=provider,
        model=model,
    )

    step = 0
    stop_reason = ""
    while True:
        stop, stop_reason = should_stop(history, step, args.budget, STATE.last_score)
        if stop:
            break

        step += 1
        STATE.model_calls = step
        try:
            active_schemas = _schemas_for_phase()
            reply = call_model(build_context(history, step), active_schemas)
        except Exception as exc:
            stop_reason = f"model error: {exc}"
            rec(event="model_error", step=step, error=str(exc))
            break
        rec(
            event="model",
            step=step,
            phase=STATE.phase,
            available_tools=[schema["name"] for schema in active_schemas],
            reply=reply,
        )

        if reply.get("text"):
            print(f"[{step}] {reply['text'][:200]}")
        calls = reply.get("tool_calls") or []
        history.append(
            {
                "role": "assistant",
                "content": reply.get("text") or "",
                "tool_calls": calls,
            }
        )

        if not calls:
            STATE.consecutive_no_tool += 1
            continue
        STATE.consecutive_no_tool = 0

        for call in calls:
            signature = json.dumps(
                {"name": call.get("name"), "arguments": call.get("arguments")},
                sort_keys=True,
            )
            if signature == STATE.last_action:
                STATE.repeated_actions += 1
            else:
                STATE.repeated_actions = 0
            STATE.last_action = signature

            tool = BY_NAME.get(call.get("name"))
            if tool is None:
                output = f"unknown tool {call.get('name')!r}"
            elif "_invalid_json" in (call.get("arguments") or {}):
                output = f"invalid tool arguments: {call['arguments']['_invalid_json']}"
            else:
                try:
                    output = tool["fn"](call.get("arguments") or {})
                except Exception as exc:
                    output = f"tool error ({type(exc).__name__}): {exc}"
            name = call.get("name")
            if name in {"write_rust", "replace_rust"} and not str(output).startswith(
                ("error:", "tool error")
            ):
                validation = _validate_after_edit()
                output = f"{output}\n\nAUTOMATIC VALIDATION:\n{validation}"
            elif name in {"read_rust", "probe"}:
                STATE.phase = "diagnose"
            elif name in {"read_source", "search_source"} and STATE.edit_attempts == 0:
                STATE.phase = "grounding"
            print(f"      -> {call.get('name')}: {str(output).splitlines()[0][:120]}")
            rec(
                event="tool",
                step=step,
                call_id=call.get("id"),
                name=call.get("name"),
                arguments=call.get("arguments"),
                output=str(output),
                state=dataclasses.asdict(STATE),
            )
            history.append(
                {
                    "role": "tool",
                    "name": call.get("name"),
                    "tool_call_id": call.get("id"),
                    "content": str(output),
                }
            )

    restored = _restore_best_if_needed()
    print(f"\n[stop] {stop_reason}")
    rec(
        event="stop",
        reason=stop_reason,
        steps=step,
        best_score=STATE.best_score,
        best_step=STATE.best_step,
        restored_best=restored,
    )
    print(f"\ntrajectory: {log}")
    print("final score:")
    subprocess.run([sys.executable, str(HERE / "evaluate.py")], cwd=HERE)
    return 0 if STATE.best_score == 100.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
