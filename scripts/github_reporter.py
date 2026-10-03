#!/usr/bin/env python3
"""
Posts the adjudicated AI review findings as a PR comment via PyGithub.

Exits 1 if any CRITICAL finding is present, so the workflow step can be
used as a required check to block merges on critical issues.
"""
import json
import os
import sys
from typing import List

from github import Github, Auth

COMMENT_MARKER = "<!-- ai-review-council:do-not-edit -->"
SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
SEVERITY_EMOJI = {"CRITICAL": "\U0001F534", "WARNING": "\U0001F7E1", "INFO": "\U0001F535"}


def load_findings(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def render_table(findings: List[dict]) -> str:
    rows = [
        "| Severity | File | Line | Agent | Description |",
        "|---|---|---|---|---|",
    ]
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", "INFO"), 3))
    for finding in ordered:
        severity = finding.get("severity", "INFO")
        emoji = SEVERITY_EMOJI.get(severity, "")
        file_path = finding.get("file", "?")
        line = finding.get("line")
        line_str = str(line) if line is not None else "-"
        agent = finding.get("agent", "?")
        description = finding.get("description", "").replace("\n", " ").replace("|", "\\|")
        rows.append(f"| {emoji} {severity} | `{file_path}` | {line_str} | {agent} | {description} |")
    return "\n".join(rows)


def render_details(findings: List[dict]) -> str:
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", "INFO"), 3))
    blocks = []
    for finding in ordered:
        severity = finding.get("severity", "INFO")
        emoji = SEVERITY_EMOJI.get(severity, "")
        file_path = finding.get("file", "?")
        line = finding.get("line")
        location = f"{file_path}:{line}" if line is not None else file_path
        description = finding.get("description", "")
        fix = finding.get("fix", "").strip()
        fix_block = f"\n\n**Suggested fix:**\n```\n{fix}\n```" if fix else ""
        blocks.append(
            f"<details>\n<summary>{emoji} <b>{severity}</b> &mdash; <code>{location}</code> "
            f"({finding.get('agent', '?')})</summary>\n\n{description}{fix_block}\n\n</details>"
        )
    return "\n\n".join(blocks)


def render_body(data: dict) -> str:
    findings = data.get("findings", [])
    summary = data.get("summary", "")

    if not findings:
        return (
            f"{COMMENT_MARKER}\n"
            "## \U0001F916 AI Review Council\n\n"
            "✅ No issues detected.\n\n"
            f"{summary}"
        )

    critical_count = sum(1 for f in findings if f.get("severity") == "CRITICAL")
    warning_count = sum(1 for f in findings if f.get("severity") == "WARNING")
    info_count = sum(1 for f in findings if f.get("severity") == "INFO")

    header = (
        f"## \U0001F916 AI Review Council\n\n"
        f"**{critical_count}** critical, **{warning_count}** warning, **{info_count}** info\n\n"
        f"{summary}\n\n"
        "### Findings\n\n"
        f"{render_table(findings)}\n\n"
        "### Details\n\n"
        f"{render_details(findings)}"
    )
    return f"{COMMENT_MARKER}\n{header}"


def upsert_comment(pr, body: str) -> None:
    for comment in pr.get_issue_comments():
        if COMMENT_MARKER in (comment.body or ""):
            comment.edit(body)
            print("Updated existing AI review comment.")
            return
    pr.create_issue_comment(body)
    print("Posted new AI review comment.")


def main() -> int:
    findings_path = os.environ.get("FINDINGS_PATH", "findings.json")
    token = os.environ.get("GITHUB_TOKEN")
    repo_full_name = os.environ.get("REPO_FULL_NAME")
    pr_number = os.environ.get("PR_NUMBER")

    if not token or not repo_full_name or not pr_number:
        print("error: GITHUB_TOKEN, REPO_FULL_NAME, and PR_NUMBER must all be set", file=sys.stderr)
        return 1

    if not os.path.exists(findings_path):
        print(f"error: findings file not found at {findings_path}", file=sys.stderr)
        return 1

    data = load_findings(findings_path)
    findings = data.get("findings", [])

    gh = Github(auth=Auth.Token(token))
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(int(pr_number))

    body = render_body(data)
    upsert_comment(pr, body)

    critical_count = sum(1 for f in findings if f.get("severity") == "CRITICAL")
    if critical_count > 0:
        print(f"Blocking: {critical_count} CRITICAL finding(s) detected.")
        return 1

    print("No critical issues detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
