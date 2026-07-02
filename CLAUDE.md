# CLAUDE.md — DotConnector Autofix Agent

Project memory and operating guide for this repository. Read this before
changing code. It documents what the system does, why it is built this way, and
the rules any contributor (human or agent) must follow.

---

## 1. What this is

An **autonomous dependency-remediation agent**. It takes vulnerability findings
from **DotConnector** (the internal scanner that lists vulnerable dependencies
with a declared version, a recommended version, severity, repository, file path,
ecosystem, and a purl), and for each fixable finding it:

1. locates the dependency in the target repo's manifest,
2. confirms the recommended version is real and the bump is safe,
3. applies the version change,
4. **validates** the change (resolves + tests pass),
5. opens a **pull request** for a human to review and merge.

A human only ever appears at **PR approval**. Everything before that is
automated, including diagnosing a failed validation and retrying.

> This repo is a **proof of concept**. It runs fully offline with a deterministic
> stub LLM, and against a free LLM API (Groq by default) for real reasoning. The
> production migration path is in §11.

---

## 2. The core design principle

**Deterministic tools are the agent's hands. The LLM is its judgment. Never mix
the two.**

- Things that have a correct answer — does this version exist? is this a major or
  minor bump? what line in the file declares this package? did the tests pass? —
  are done by **deterministic Python tools** (`src/tools/`). They are fast, free,
  and identical every run, which is what makes a security automation auditable.
- Things that need judgment — is the recommended version actually a sensible
  target here? this validation failed, what should we try next? how do I explain
  this change to a reviewer? — are delegated to the **LLM** (`src/llm.py`).

The LLM is kept on a short leash: strict JSON in/out, a per-finding call budget,
and it can only ever *propose* a version. The registry and the validator decide
whether that proposal is real and safe. A hallucinated version cannot reach a PR.

This is **not** "one big agent with a pile of tools." It is a deterministic
pipeline with a few well-defined judgment calls inside a self-healing loop.

---

## 3. The remediation loop

This is the loop, per finding (`src/agent.py::remediate`):

```
ingest finding
  → triage:  waived?            → SKIP
             manifest present?  → no  → ESCALATE
             recommendation?    → no  → ESCALATE
             version real?      → no  → ESCALATE (asks the registry)
  → classify bump (patch / minor / major)        [deterministic]
  → policy gate (auto / draft-human / escalate)  [deterministic, config-driven]
  → LLM: plan_fix  → choose/confirm target version
  → self-healing loop (≤ max_attempts):
        apply version  →  VALIDATE  ──pass──→ open PR  ✅
               ▲                     ──fail──→ LLM: diagnose → retry
               └────────────── restore file, try next version ───┘
  → attempts exhausted / not auto-fixable → ESCALATE (draft PR or report)
```

The **self-healing** part is the point: when validation fails, the agent feeds
the failure to the LLM, gets a new candidate version, resets the file, and tries
again — bounded by `max_attempts`. It escalates only when it genuinely cannot fix
the issue, not on the first red check.

The human shows up only at "open PR" (review & merge) and at "escalate".

---

## 4. The canonical Finding

Every upstream payload is normalized to this (`src/models.py::Finding`) so the
rest of the system never deals with DotConnector's raw shape:

| field                 | meaning                                              |
|-----------------------|------------------------------------------------------|
| `finding_id`          | scanner's id, used in branch/PR/audit                |
| `repository`          | which repo to fix                                    |
| `ecosystem`           | pypi / maven / npm / …                               |
| `package`             | dependency name                                      |
| `declared_version`    | what the repo currently pins                         |
| `recommended_version` | scanner's suggested fix (may be null)                |
| `severity`            | critical / high / medium / low                       |
| `cve_ids`             | advisories, for the PR body                          |
| `file_path`           | a hint only — we re-locate to be safe                |
| `purl`                | the cross-ecosystem identity key                     |
| `waived`              | if true, do nothing                                  |

`Finding.fingerprint` = `repository :: purl :: target_version`. This is the
**idempotency key**: two runs that would make the same change produce the same
fingerprint, so we never open duplicate PRs.

---

## 5. What counts as a passing check (read this)

A change becomes a PR **only if every required check passes**
(`src/tools/validator.py`). This is the gate, in order:

1. **`manifest_parses`** — after the edit, the manifest is still syntactically
   valid (every requirement line still parses).
2. **`version_resolvable`** — the dependency set resolves with no conflict and
   every pinned version exists in the registry.
   - *POC:* a registry-existence proxy — every `==` pin must exist on PyPI.
   - *Prod:* the **real ecosystem resolver** (e.g. `pip install --dry-run`, `uv
     pip compile`, Maven/Gradle resolve), run in CI on the PR. See
     `.github/workflows/pr-gate.yml`.
3. **`tests_pass`** — the target repo's own test suite passes after the bump
   (`python -m pytest -q`, exit 0).

Optional / policy checks layered on top:

4. **`no_severity_regression`** — a re-scan shows the new version is not itself
   flagged at ≥ the original severity. (Documented hook; wire to DotConnector in
   prod so a "fix" can't introduce a worse advisory.)
5. **`lockfile_consistent`** — any lockfile is regenerated and matches.

If a required check fails, the agent does **not** open a normal PR. It retries
(loop) or escalates as a **draft** PR with the failing output attached, so a
human sees exactly what broke. The gate is then **re-run in real CI** on the PR
itself (defense in depth) before anyone can merge.

---

## 6. Policy — what gets fixed automatically

Config-driven and deterministic (`config.yaml` → `src/policy.py`). Defaults:

| situation                              | action                          |
|----------------------------------------|---------------------------------|
| waived                                 | **skip**                        |
| no recommended version                 | **escalate** (needs-human)      |
| downgrade / same / unparseable bump    | **escalate** (needs-human)      |
| **patch** or **minor** bump            | **auto** → normal PR            |
| **major** bump                         | **draft PR**, needs-human       |
| not present in this repo's ecosystem   | **escalate**                    |

Knobs (`config.yaml`):

- `auto_deltas: [patch, minor]` — bump sizes eligible for auto-fix
- `allow_major_auto: false` — never auto-apply majors without a human
- `max_attempts: 3` — bounded self-healing retries
- `max_prs_per_run: 10` — blast-radius cap
- `max_llm_calls_per_finding: 6` — cost guard

Change policy **here**, never inline in code, so it stays auditable.

---

## 7. Safety rules (non-negotiable)

These are the guardrails that make unattended remediation acceptable:

- **Never auto-merge.** The agent opens (and drafts) PRs. Merge requires a human
  via CODEOWNERS review. The PR gate explicitly does not merge.
- **Bounded retries.** `max_attempts` caps the self-healing loop. No infinite
  loops, no runaway behavior.
- **Cost budget.** `max_llm_calls_per_finding` caps LLM calls per finding; the
  client raises when the budget is exhausted.
- **Idempotency.** Dedup by `Finding.fingerprint` before doing work; re-running
  is safe and will not open duplicate PRs.
- **Blast radius.** `max_prs_per_run` caps how many PRs a single run can open.
- **Registry is the source of truth.** The agent never assumes a version exists;
  it asks. Hallucinated versions are rejected before any edit reaches a PR.
- **Reversible edits.** Manifest edits are format-preserving and restorable; the
  loop resets the file between attempts and the working tree is clean afterward.
- **Audit log.** Every decision is recorded on `RemediationResult.audit` and
  written to `.run/report.json` — you can reconstruct exactly why the agent did
  what it did.
- **Least privilege in CI.** The workflow grants only `contents: write` +
  `pull-requests: write`.

---

## 8. Repository layout

```
dotconnector-autofix/
├── CLAUDE.md                  # this file
├── README.md                  # quickstart
├── run_poc.py                 # E2E entrypoint
├── config.yaml                # policy
├── .env.example               # LLM provider config (free options)
├── requirements.txt
├── src/
│   ├── models.py              # Finding / CheckResult / RemediationResult
│   ├── policy.py              # auto vs draft vs escalate
│   ├── llm.py                 # provider-agnostic LLM + StubLLM (judgment)
│   ├── agent.py               # the self-healing loop (orchestrator)
│   └── tools/                 # deterministic "hands"
│       ├── registry.py        # PyPI client (real, free)
│       ├── semver_tool.py     # bump classification
│       ├── manifest.py        # locate + edit dependency files
│       ├── validator.py       # the passing-check gate
│       └── github_pr.py       # PR creation (local + github modes)
├── mock_dotconnector/
│   ├── findings.json          # simulated findings (from the screenshots)
│   └── server.py              # loader + optional FastAPI + webhook
├── sample_repo/               # the target repo the agent fixes
│   ├── requirements.txt       # vulnerable pins matching the findings
│   ├── src/redactor.py        # dependency-free code under test
│   └── tests/                 # the tests_pass gate
├── .github/workflows/
│   ├── remediate.yml          # event-driven + nightly agent run
│   └── pr-gate.yml            # re-runs the passing check on the PR
└── tests/                     # unit tests for the deterministic tools
```

---

## 9. How to run

**Offline (no keys, deterministic stub LLM) — works out of the box:**

```bash
pip install -r requirements.txt
python run_poc.py
```

You'll see every path exercised: waived → skip, duplicate → deduped, no
recommendation / wrong ecosystem → escalate, minor bumps → PR, major bump →
draft PR. PR bodies land in `.run/prs/`, the audit report in `.run/report.json`,
and real git branches in `.run/workspace` (`git -C .run/workspace branch`).

**With a real free LLM (Groq, recommended for dev):**

```bash
cp .env.example .env
# set LLM_PROVIDER=groq and GROQ_API_KEY=... (free, no credit card,
# from https://console.groq.com)
python run_poc.py
```

**Run the mock DotConnector API (optional):**

```bash
uvicorn mock_dotconnector.server:app --port 8800
# GET http://localhost:8800/applications/600004364/dependencies
# POST http://localhost:8800/webhook   (mimics the repository_dispatch trigger)
```

**Tests:** `python -m pytest -q tests/`

---

## 10. LLM provider configuration

The LLM is behind one interface (`src/llm.py::LLM`) with two implementations:
`StubLLM` (offline, deterministic) and `OpenAICompatLLM` (any OpenAI-compatible
endpoint). `make_llm()` builds one from env and **falls back to the stub** if no
key is set, so the pipeline always runs.

Free providers (set in `.env`):

| `LLM_PROVIDER` | default model                              | key env             |
|----------------|--------------------------------------------|---------------------|
| `groq`         | `llama-3.3-70b-versatile`                  | `GROQ_API_KEY`      |
| `gemini`       | `gemini-2.0-flash`                         | `GEMINI_API_KEY`    |
| `openrouter`   | `meta-llama/llama-3.3-70b-instruct:free`   | `OPENROUTER_API_KEY`|
| `ollama`       | `llama3.1` (fully local)                   | — (none)            |
| `stub`         | n/a (offline)                              | — (none)            |

Override the model with `LLM_MODEL`. For high request volume on Groq, switch to
`llama-3.1-8b-instant`. Model IDs on free tiers change — check the provider
console if one is retired.

---

## 11. Production migration

This POC is deliberately free and self-contained. To productionize:

1. **LLM: free provider → Claude.** Swap to the **Claude Agent SDK** (Python
   3.10+) or the Anthropic API. Keep routine fixers on a cheaper model (e.g.
   Haiku) and reserve a stronger model (Sonnet/Opus) for hard diagnoses. The
   agent code does not change — only the LLM implementation behind the interface.
   - **Cost note:** since **15 June 2026**, non-interactive Agent SDK / Claude
     Code GitHub Actions runs draw from a separate monthly Agent SDK credit at
     API token rates with no rollover. Budget for it and prefer cheaper models on
     the routine path. Verify current details in Anthropic's docs before rollout.
2. **Findings: mock → real DotConnector.** Replace `mock_dotconnector` with a
   client to the real API and a normalizer into `Finding`. Trigger via
   DotConnector's webhook → GitHub `repository_dispatch` (already wired in
   `remediate.yml`), with the nightly cron as backstop.
3. **PRs: local → github mode.** Run with `--in-place --mode github` (the
   workflow already does). The built-in `GITHUB_TOKEN` with the granted scopes
   pushes branches and opens PRs. Enforce **CODEOWNERS review** — that is what
   prevents merge.
4. **Validation: proxy → real resolver.** Keep the registry existence check as a
   fast pre-filter, but make the authoritative `version_resolvable` check the
   real ecosystem resolver in `pr-gate.yml` (pip/uv, Maven, Gradle, npm). Add the
   `no_severity_regression` re-scan against DotConnector.
5. **Observability.** Ship `.run/report.json` to your logging/audit store; alert
   on escalations and on repeated failed attempts for the same package.

Honest scoping: for single-ecosystem mechanical bumps, Dependabot/Renovate
already do most of this. The value here is the **cross-language judgment layer**
— deciding when a recommended version is actually safe, diagnosing failures, and
remediating consistently across pypi/maven/npm from one DotConnector feed.

---

## 12. Extending to a new ecosystem

The seams are explicit. To add, say, Maven:

1. `src/tools/registry.py` — add a Maven Central client (`list_versions`,
   `version_exists`). The façade and the agent don't change.
2. `src/tools/manifest.py` — add `pom.xml` locate/edit (format-preserving,
   reversible), and register its glob in `MANIFEST_GLOBS`.
3. `src/tools/validator.py` — point the resolver/test commands at the Maven
   toolchain.
4. Add the ecosystem to `src/models.py::Ecosystem` (already enumerated).

`semver_tool.py`, `policy.py`, `agent.py`, and the PR layer are ecosystem-
agnostic and need no changes.

---

## 13. Conventions for working in this repo

- **Keep determinism out of the LLM.** If a question has a correct answer, write
  a tool for it; do not ask the model. New judgment calls go through `src/llm.py`
  with strict JSON and a call-budget check.
- **Every new decision must be logged** to `RemediationResult.audit`.
- **Edits stay reversible.** Anything that mutates a manifest must support
  restore so the retry loop and clean-tree guarantee keep working.
- **Respect the safety rails in §7.** Never add an auto-merge path. Never let a
  version reach a PR without passing through the registry check and the validator.
- **Policy lives in `config.yaml`,** not in code branches.
- **Tests:** deterministic tools must have unit tests (`tests/`); they must not
  hit the network or the LLM.
