# AI Multi-Agent PR Reviewer

Serverless GitHub Action that reviews every pull request with a council of
specialized LLM agents (AppSec, Concurrency & Lifecycle, Boundary & QA),
adjudicates their findings to prune false positives, and posts a single
Markdown summary comment on the PR.

## Architecture

```
pull_request event
      │
      ▼
.github/workflows/ai-review.yml
      │  checkout + diff
      ▼
scripts/review_council.py
      │
      ├── AppSec Agent ───────────────┐
      ├── Concurrency Agent  ─────────┼──► asyncio.gather (parallel)
      ├── Boundary & QA Agent ────────┘
      │
      ▼
  Adjudicator Agent (prunes false positives, dedupes, assigns severity)
      │
      ▼
  findings.json
      │
      ▼
scripts/github_reporter.py ──► posts/updates PR comment, exits 1 on CRITICAL
```

## Setup

### 1. Add repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**
and add:

| Secret name      | Value                                                              |
|-------------------|---------------------------------------------------------------------|
| `GEMINI_API_KEY` | Your Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) |

`GITHUB_TOKEN` is provided automatically by GitHub Actions — no setup needed,
as long as the workflow has `pull-requests: write` permission (already set
in `ai-review.yml`).

### 2. Confirm Actions permissions

**Settings → Actions → General → Workflow permissions**: ensure
"Read and write permissions" is allowed, or that the default
`GITHUB_TOKEN` permissions aren't restricted below `pull-requests: write`.

### 3. (Optional) Require the check to pass

**Settings → Branches → Branch protection rules**: add the `review` job
as a required status check if you want CRITICAL findings to block merges.
The reporter script exits with status `1` when any CRITICAL finding survives
adjudication, which fails the job.

### 4. Drop in the files

Copy this repo's contents into the target repository's root:

```
.github/workflows/ai-review.yml
config/rules.json
scripts/review_council.py
scripts/github_reporter.py
requirements.txt
```

Open a PR — the workflow runs automatically on `opened`, `synchronize`, and
`reopened` events.

## Configuration

- **`config/rules.json`** — per-file-extension focus hints injected into
  reviewer prompts (e.g. Swift retain-cycle patterns, Go goroutine leaks).
  Add an extension entry to tune focus for languages not yet covered.
- **`GEMINI_MODEL`** env var (optional, set in the workflow or as a repo
  variable) — overrides the default model (`gemini-2.5-flash`).
- **`max_diff_chars`** in `rules.json` — truncates very large diffs before
  sending them to the LLM to control cost/latency.

## Local testing

```bash
pip install -r requirements.txt

export GEMINI_API_KEY=...
export DIFF_PATH=pr.diff
export RULES_PATH=config/rules.json
export FINDINGS_PATH=findings.json

git diff main...my-branch > pr.diff
python scripts/review_council.py
cat findings.json
```

To test the reporter against a real PR without re-running the LLM:

```bash
export GITHUB_TOKEN=ghp_...
export REPO_FULL_NAME=owner/repo
export PR_NUMBER=123
export FINDINGS_PATH=findings.json

python scripts/github_reporter.py
```

## Notes

- Re-running the workflow on `synchronize` (new pushes) updates the same PR
  comment in place rather than spamming new ones.
- Each specialist agent is scoped to a narrow concern on purpose — this
  keeps prompts focused and lets the adjudicator reason about overlap/false
  positives explicitly rather than relying on one generalist pass.
- Findings are also uploaded as a workflow artifact (`ai-review-findings`)
  for auditing outside the PR comment.
