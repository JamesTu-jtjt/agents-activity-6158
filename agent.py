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
    last_score: float | None = None
    best_step: int | None = None
    last_evaluation: dict | None = None
    evaluations_without_improvement: int = 0
    consecutive_no_tool: int = 0
    repeated_actions: int = 0
    last_action: str | None = None
    model_calls: int = 0


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


def call_model(messages: list[dict], tools: list[dict]) -> dict:
    """Call Gemini or OpenAI directly and normalize its function-call reply."""
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"model API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"model API request failed: {exc}") from exc

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
prefixes sort first. The assignment's bump_major/minor/patch functions are the
corresponding Version methods in reference/version.py, not next_version.

Use evaluate after the code builds and tests pass. Treat its structured result
as the reward signal, but do not tune only to seed 0. A task is complete only
at 100% differential correctness, a passing precedence chain and cargo tests,
and zero quality violations. If an edit regresses, the harness restores the
best validated library. Do not merely announce completion: prove it with the
evaluate tool."""


def _compact_tool_content(message: dict) -> str:
    content = message.get("content", "")
    limit = 7000 if message.get("name") in {"read_rust", "evaluate"} else 3500
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
        "last_score": STATE.last_score,
        "best_step": STATE.best_step,
        "evaluations_without_improvement": STATE.evaluations_without_improvement,
        "last_evaluation": STATE.last_evaluation,
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
    binary = RUST / "target" / "release" / "harness"
    if not binary.exists():
        return "error: release harness missing; run cargo_build first"
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


def t_evaluate(_args: dict) -> str:
    """Evaluate three local seeds and preserve or restore the best library."""
    STATE_DIR.mkdir(exist_ok=True)
    per_seed = []
    for seed in (0, 17, 271):
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
                    "120",
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
            if marker in run["output"]:
                one["first_failures"] = run["output"].split(marker, 1)[1].strip()[:5000]
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
    STATE.last_score = score
    STATE.last_evaluation = summary
    improved = score is not None and (STATE.best_score is None or score > STATE.best_score)
    if improved:
        STATE.best_score = score
        STATE.best_step = STATE.model_calls
        STATE.evaluations_without_improvement = 0
        shutil.copyfile(LIB, BEST_LIB)
        summary["best_revision"] = "updated"
    else:
        STATE.evaluations_without_improvement += 1
        if (
            score is not None
            and STATE.best_score is not None
            and score < STATE.best_score
            and BEST_LIB.exists()
        ):
            shutil.copyfile(BEST_LIB, LIB)
            summary["best_revision"] = "restored after regression"
        else:
            summary["best_revision"] = "unchanged"
    summary["best_score"] = STATE.best_score
    return json.dumps(summary, indent=2)


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
        description="Overwrite lib.rs. Use for an initial implementation or coherent full rewrite.",
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
        description="Replace one exact, uniquely occurring fragment in lib.rs.",
        parameters={
            "type": "object",
            "required": ["old", "new"],
            "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
            "additionalProperties": False,
        },
        fn=t_replace_rust,
    ),
    dict(name="cargo_build", description="Build the release harness.", parameters=_empty_schema(), fn=t_cargo_build),
    dict(name="cargo_test", description="Run Rust unit tests in release mode.", parameters=_empty_schema(), fn=t_cargo_test),
    dict(
        name="probe",
        description="Run 1-30 focused harness commands: parse, compare, bump, or format.",
        parameters={
            "type": "object",
            "required": ["commands"],
            "properties": {"commands": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False,
        },
        fn=t_probe,
    ),
    dict(
        name="evaluate",
        description="Run structured evaluation on local seeds 0, 17, and 271; preserve best and roll back regressions.",
        parameters=_empty_schema(),
        fn=t_evaluate,
    ),
]
BY_NAME = {tool["name"]: tool for tool in TOOLS}
SCHEMAS = [{key: tool[key] for key in ("name", "description", "parameters")} for tool in TOOLS]


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
            reply = call_model(build_context(history, step), SCHEMAS)
        except Exception as exc:
            stop_reason = f"model error: {exc}"
            rec(event="model_error", step=step, error=str(exc))
            break
        rec(event="model", step=step, reply=reply)

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
