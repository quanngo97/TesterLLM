#!/usr/bin/env python3
"""
Multi-agent AI code review council.

Runs three specialized reviewer agents concurrently against a PR diff,
then has an adjudicator agent prune false positives and emit the final
findings JSON consumed by github_reporter.py.
"""
import asyncio
import json
import os
import re
import sys
from typing import List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2


class Finding(BaseModel):
    file: str = Field(description="Path to the affected file")
    line: Optional[int] = Field(default=None, description="1-indexed line number, if known")
    severity: str = Field(description="One of CRITICAL, WARNING, INFO")
    agent: str = Field(description="Name of the specialist agent that raised this")
    description: str = Field(description="Clear explanation of the bug")
    fix: str = Field(default="", description="Suggested code snippet or diff to resolve it")


class CouncilOutput(BaseModel):
    findings: List[Finding] = Field(default_factory=list)


class AdjudicatorOutput(BaseModel):
    has_issues: bool
    summary: str
    findings: List[Finding] = Field(default_factory=list)


REVIEWER_AGENTS = [
    {
        "name": "AppSec Agent",
        "instructions": (
            "You are an application security specialist reviewing a pull request diff. "
            "Audit strictly for: hardcoded credentials or secret leaks, OWASP Top 10 risks "
            "(injection, broken access control, SSRF, insecure deserialization), authentication "
            "and authorization flaws, and unsafe use of cryptography. "
            "Ignore style, performance, and anything unrelated to security. "
            "Only report issues clearly introduced or present in the diff's added/modified lines. "
            "If nothing security-relevant is found, return an empty findings list."
        ),
    },
    {
        "name": "Concurrency Specialist",
        "instructions": (
            "You are a concurrency and object-lifecycle specialist reviewing a pull request diff. "
            "Audit strictly for: race conditions, thread-safety violations, retain cycles / memory "
            "leaks (e.g. missing [weak self] in Swift closures, listeners never removed in JS/TS, "
            "unclosed streams or connections in Go/Node/Java), unstructured or leaked goroutines/"
            "coroutines/tasks, deadlocks, and improper cancellation handling. "
            "Ignore style, security, and null-safety issues outside this scope. "
            "If nothing in this scope is found, return an empty findings list."
        ),
    },
    {
        "name": "Boundary & QA Agent",
        "instructions": (
            "You are a defensive-programming and QA specialist reviewing a pull request diff. "
            "Audit strictly for: unhandled nil/null/undefined, missing error-handling branches "
            "(especially unchecked error returns and unhandled network/IO failure paths), "
            "out-of-bounds or off-by-one array/slice access, and unvalidated external input "
            "that can crash the program. "
            "Ignore style, security, and concurrency issues outside this scope. "
            "If nothing in this scope is found, return an empty findings list."
        ),
    },
]

ADJUDICATOR_INSTRUCTIONS = (
    "You are the adjudicator for a council of specialist code reviewers. You will receive the "
    "original PR diff and the raw candidate findings from three specialist agents (AppSec, "
    "Concurrency, Boundary & QA). Your job:\n"
    "1. Merge all candidate findings.\n"
    "2. Prune false positives: drop any finding that is already mitigated by an existing guard "
    "clause, null-check, framework invariant (e.g. SwiftUI's automatic main-thread dispatch, "
    "a context that is clearly cancelled elsewhere, an ORM that already parameterizes queries), "
    "or that misreads the diff.\n"
    "3. Deduplicate overlapping findings from different agents into a single entry, keeping the "
    "most precise description and the most severe applicable severity.\n"
    "4. Assign severity: CRITICAL only for issues with a plausible, concrete path to a crash, "
    "data breach, data corruption, or production outage. WARNING for real but lower-impact bugs. "
    "INFO for minor or speculative concerns worth a human glance.\n"
    "5. Write a concise, high-level summary of what was found (or confirm the diff looks clean).\n"
    "Return strict JSON matching the required schema only — no commentary outside the schema."
)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def load_rules(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"warning: could not load rules from {path}: {exc}", file=sys.stderr)
        return {"defaults": {"max_diff_chars": 60000}, "extensions": {}}


def extract_changed_extensions(diff_text: str) -> List[str]:
    paths = re.findall(r"^\+\+\+ b/(.+)$", diff_text, flags=re.MULTILINE)
    exts = sorted({os.path.splitext(p)[1] for p in paths if os.path.splitext(p)[1]})
    return exts


def build_focus_hints(extensions: List[str], rules: dict) -> str:
    ext_rules = rules.get("extensions", {})
    lines = []
    for ext in extensions:
        rule = ext_rules.get(ext)
        if not rule:
            continue
        focus_list = "\n    - ".join(rule.get("focus", []))
        lines.append(f"  {ext} ({rule.get('language', ext)}):\n    - {focus_list}")
    if not lines:
        return ""
    return "Language-specific focus hints for files changed in this diff:\n" + "\n".join(lines)


def truncate_diff(diff_text: str, max_chars: int) -> str:
    if len(diff_text) <= max_chars:
        return diff_text
    truncated = diff_text[:max_chars]
    return truncated + f"\n\n[... diff truncated at {max_chars} characters ...]"


async def call_with_retry(client: genai.Client, prompt: str, system_instruction: str, response_schema):
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=response_schema,
        temperature=0.1,
    )
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=config,
            )
            return response.text
        except Exception as exc:  # noqa: BLE001 - broad: any transient API/network failure
            last_exc = exc
            print(f"attempt {attempt}/{MAX_RETRIES} failed: {exc}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
    raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts") from last_exc


async def run_reviewer_agent(client: genai.Client, agent: dict, diff_text: str, focus_hints: str) -> CouncilOutput:
    prompt = (
        f"{agent['instructions']}\n\n"
        f"{focus_hints}\n\n"
        "Review the following unified diff. Only comment on lines that were added or modified "
        "(lines starting with '+' in the diff, excluding the '+++' file header). Reference file "
        "paths exactly as they appear in the diff headers.\n\n"
        f"--- BEGIN DIFF ---\n{diff_text}\n--- END DIFF ---"
    )
    try:
        raw = await call_with_retry(client, prompt, agent["instructions"], CouncilOutput)
        parsed = CouncilOutput.model_validate_json(raw)
        for finding in parsed.findings:
            finding.agent = agent["name"]
        return parsed
    except Exception as exc:  # noqa: BLE001
        print(f"error: {agent['name']} failed permanently: {exc}", file=sys.stderr)
        return CouncilOutput(findings=[])


async def run_adjudicator(client: genai.Client, diff_text: str, candidates: List[Finding]) -> AdjudicatorOutput:
    candidates_json = json.dumps([c.model_dump() for c in candidates], indent=2)
    prompt = (
        f"{ADJUDICATOR_INSTRUCTIONS}\n\n"
        f"--- BEGIN DIFF ---\n{diff_text}\n--- END DIFF ---\n\n"
        f"--- BEGIN CANDIDATE FINDINGS ---\n{candidates_json}\n--- END CANDIDATE FINDINGS ---"
    )
    try:
        raw = await call_with_retry(client, prompt, ADJUDICATOR_INSTRUCTIONS, AdjudicatorOutput)
        return AdjudicatorOutput.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"error: adjudicator failed permanently: {exc}", file=sys.stderr)
        # Fail safe: surface unfiltered candidates rather than silently dropping a real run.
        return AdjudicatorOutput(
            has_issues=bool(candidates),
            summary="Adjudicator failed; showing unfiltered candidate findings from specialist agents.",
            findings=candidates,
        )


async def main_async() -> int:
    diff_path = os.environ.get("DIFF_PATH", "pr.diff")
    rules_path = os.environ.get("RULES_PATH", "config/rules.json")
    findings_path = os.environ.get("FINDINGS_PATH", "findings.json")
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        print("error: GEMINI_API_KEY is not set", file=sys.stderr)
        return 1

    if not os.path.exists(diff_path):
        print(f"error: diff file not found at {diff_path}", file=sys.stderr)
        return 1

    diff_text = load_text(diff_path)
    if not diff_text.strip():
        print("No diff content found; nothing to review.")
        with open(findings_path, "w", encoding="utf-8") as f:
            json.dump({"has_issues": False, "summary": "No changes to review.", "findings": []}, f, indent=2)
        return 0

    rules = load_rules(rules_path)
    max_chars = rules.get("defaults", {}).get("max_diff_chars", 60000)
    diff_text = truncate_diff(diff_text, max_chars)

    extensions = extract_changed_extensions(diff_text)
    focus_hints = build_focus_hints(extensions, rules)

    client = genai.Client(api_key=api_key)

    reviewer_results = await asyncio.gather(
        *[run_reviewer_agent(client, agent, diff_text, focus_hints) for agent in REVIEWER_AGENTS]
    )

    all_candidates: List[Finding] = []
    for result in reviewer_results:
        all_candidates.extend(result.findings)

    print(f"Collected {len(all_candidates)} raw candidate findings from {len(REVIEWER_AGENTS)} agents.")

    if not all_candidates:
        final = AdjudicatorOutput(has_issues=False, summary="No issues found by any reviewer agent.", findings=[])
    else:
        final = await run_adjudicator(client, diff_text, all_candidates)

    with open(findings_path, "w", encoding="utf-8") as f:
        json.dump(final.model_dump(), f, indent=2)

    print(f"Wrote {len(final.findings)} adjudicated finding(s) to {findings_path}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
