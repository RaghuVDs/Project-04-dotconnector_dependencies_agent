# DotConnector Autofix Agent (POC)

An autonomous agent that reads vulnerable-dependency findings from **DotConnector**,
fixes the version in the repo's manifest, validates the change (resolves + tests
pass), and opens a **pull request** for a human to approve. The agent diagnoses
failed checks and retries on its own; a human only appears at PR approval.

It runs **fully offline out of the box** (deterministic stub LLM) and against a
**free LLM API** (Groq by default) for real reasoning.

See **[CLAUDE.md](./CLAUDE.md)** for the full architecture, the design rationale,
the exact passing-check definition, safety rules, and the production migration
path.

## Quickstart

```bash
pip install -r requirements.txt
python run_poc.py
```

That simulates DotConnector, copies `sample_repo/` into a clean git workspace,
and remediates each finding. Output:

```
[F-0005] PR    flask 3.0.0 -> 3.1.3      checks passed; PR opened
[F-0006] HUMAN pytest 8.2.0 -> 9.0.3     draft PR (major bump)
[F-0008] PR    requests 2.31.0 -> 2.33.1 checks passed; PR opened
[F-0010] PR    werkzeug 3.0.6 -> 3.1.6   checks passed; PR opened
...
SUMMARY: escalated=5  fixed=3  skipped=1   PRs/branches opened: 4
```

Artifacts:
- `.run/prs/` — generated PR bodies (with diffs)
- `.run/report.json` — full audit log of every decision
- `.run/workspace` — real git branches (`git -C .run/workspace branch`)

## Use a real free LLM

```bash
cp .env.example .env        # set LLM_PROVIDER=groq + GROQ_API_KEY (free, no card)
python run_poc.py
```

Groq, Gemini, OpenRouter, and local Ollama are all supported via the same
OpenAI-compatible interface — see `.env.example`.

## What's inside

| path                     | what                                             |
|--------------------------|--------------------------------------------------|
| `src/agent.py`           | the self-healing remediation loop                |
| `src/tools/`             | deterministic tools (registry, semver, manifest, validator, PR) |
| `src/llm.py`             | provider-agnostic LLM judgment + offline stub    |
| `src/policy.py`          | auto-fix vs draft vs escalate                    |
| `mock_dotconnector/`     | simulated scanner (JSON + optional API/webhook)  |
| `sample_repo/`           | the target repo the agent fixes                  |
| `.github/workflows/`     | event-driven agent run + PR gate                 |

## Tests

```bash
python -m pytest -q tests/
```
