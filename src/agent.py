"""The agent: the self-healing remediation loop for ONE finding.

This is the loop from the architecture diagram, in code:

    triage -> locate -> registry check -> classify -> policy gate
        -> [ apply fix -> validate ] --pass--> open PR
                  ^                  --fail--> diagnose -> retry (<= N)
                  |__________________________________________|
        -> exhausted / not auto-fixable -> escalate (draft PR / report)

Deterministic tools do the work; the LLM supplies judgment at plan/diagnose.
The human only ever sees the result as a PR to approve -- the agent never merges.
"""
from __future__ import annotations

from pathlib import Path

from .llm import LLM
from .models import Ecosystem, Finding, Outcome, RemediationResult
from .policy import Action, PolicyConfig, ValidationConfig, decide
from .tools import manifest as manifest_tool
from .tools.github_pr import PRManager, render_pr_body
from .tools.registry import Registry, RegistryError
from .tools.semver_tool import Delta, classify, previous_versions
from .tools.validator import VenvCreateError, VenvSession


def remediate(
    finding: Finding,
    repo_dir: Path,
    base_branch: str,
    registry: Registry,
    llm: LLM,
    pr: PRManager,
    cfg: PolicyConfig,
    vcfg: ValidationConfig,
    venv: VenvSession | None = None,
) -> RemediationResult:
    res = RemediationResult(
        finding_id=finding.finding_id, package=finding.package,
        outcome=Outcome.ERROR, from_version=finding.declared_version,
    )
    res.log(f"start: {finding.package} {finding.declared_version} "
            f"-> {finding.recommended_version} ({finding.severity.value})")
    llm.reset_budget()  # the call cap is per-finding, not per-run

    # --- triage: cheap deterministic exits ---------------------------------
    if finding.waived:
        res.outcome = Outcome.SKIPPED
        res.reason = "waived"
        res.log("skip: finding is waived")
        return res

    located = manifest_tool.locate(repo_dir, finding.ecosystem, finding.package)
    if located is None:
        res.outcome = Outcome.ESCALATED
        res.reason = (f"no {finding.ecosystem.value} manifest declaring "
                      f"{finding.package} in this repo")
        res.log("escalate: " + res.reason)
        return res
    manifest_path, declared = located
    res.from_version = declared
    res.log(f"located in {manifest_path.relative_to(repo_dir)} (declared {declared})")

    if not finding.recommended_version:
        res.outcome = Outcome.ESCALATED
        res.reason = "no recommended version provided by scanner"
        res.log("escalate: " + res.reason)
        return res

    # registry truth check + delta classification (never guessed)
    if not registry.version_exists(finding.ecosystem, finding.package,
                                   finding.recommended_version):
        res.outcome = Outcome.ESCALATED
        res.reason = (f"recommended version {finding.recommended_version} "
                      f"not found in registry")
        res.log("escalate: " + res.reason)
        return res
    delta = classify(declared, finding.recommended_version)
    res.log(f"bump classified as {delta.value}")

    # --- policy gate -------------------------------------------------------
    decision = decide(finding, delta, cfg)
    res.log(f"policy: {decision.action.value} -- {decision.reason}")
    if decision.action == Action.SKIP:
        res.outcome = Outcome.SKIPPED
        res.reason = decision.reason
        return res
    if decision.action == Action.ESCALATE:
        res.outcome = Outcome.ESCALATED
        res.reason = decision.reason
        return res
    draft = decision.action == Action.DRAFT_HUMAN

    # --- LLM judgment: confirm/choose target -------------------------------
    try:
        versions = registry.list_versions(finding.ecosystem, finding.package)
    except RegistryError:
        versions = []
    excerpt = manifest_tool.excerpt(manifest_path, finding.package)
    try:
        plan = llm.plan_fix(finding, delta, excerpt, versions)
    except Exception as exc:  # noqa: BLE001 - LLM is an external dep; never crash the run
        # judgment is unavailable (auth/rate-limit/network/budget) -> the
        # deterministic safety net escalates this finding for a human and the
        # run continues with the next one. A bad key must not abort everything.
        res.outcome = Outcome.ESCALATED
        res.reason = (f"LLM judgment unavailable ({exc.__class__.__name__}); "
                      f"escalated for human review")
        res.log(f"escalate: LLM call failed: {exc.__class__.__name__}: {exc}")
        return res
    res.log(f"plan: target={plan.target_version} auto_fixable={plan.auto_fixable} "
            f":: {plan.reasoning}")
    if not plan.auto_fixable and not draft:
        res.outcome = Outcome.ESCALATED
        res.reason = f"agent judged not auto-fixable: {plan.reasoning}"
        return res
    target = plan.target_version or finding.recommended_version
    if not registry.version_exists(finding.ecosystem, finding.package, target):
        target = finding.recommended_version  # guard against a hallucinated target
        res.log(f"target not in registry; falling back to recommended {target}")

    # --- deterministic candidate sequence ----------------------------------
    # Try the recommended version first; if it fails validation, step DOWN the
    # version ladder -- the previous release, then the one before that -- staying
    # strictly above the declared pin (so we only ever try other *upgrades*). The
    # ladder is chosen deterministically from the registry, never by the LLM, so
    # the retry is auditable. If every candidate fails, the manifest is restored
    # to the declared version (nothing broken, nothing shipped) and a comment is
    # attached. NOTE: stepped-down versions are BELOW the scanner recommendation
    # and may not remediate the advisory -- they open as drafts (see below).
    candidates: list[str] = []

    def _add_candidate(version: str | None) -> None:
        if (version and version not in candidates
                and registry.version_exists(finding.ecosystem, finding.package, version)):
            candidates.append(version)

    _add_candidate(target)
    if cfg.fallback_to_previous:
        # step down from the recommended target, floored at the declared version
        for older in previous_versions(versions, target, floor=declared,
                                       count=cfg.max_attempts):
            _add_candidate(older)
    candidates = candidates[:cfg.max_attempts]  # respect the bounded-retry cap
    res.log(f"candidates (in order, recommended first): "
            f"{', '.join(candidates) or 'none'}")

    # --- self-healing loop -------------------------------------------------
    last_check = None
    for attempt, candidate in enumerate(candidates, 1):
        res.attempts = attempt
        edit = manifest_tool.set_version(manifest_path, finding.package, candidate)
        res.log(f"attempt {attempt}: set {finding.package}=={candidate}")

        from .tools.validator import validate  # local import avoids cycle at import
        try:
            check = validate(repo_dir, manifest_path, finding.ecosystem, registry,
                             vcfg, venv)
        except VenvCreateError as exc:
            edit.restore()  # leave a clean tree even on infra failure
            res.outcome = Outcome.ERROR
            res.reason = f"validation environment unavailable: {exc}"
            res.log("error: " + res.reason)
            return res
        last_check = check
        res.log(f"attempt {attempt}: checks={check.checks} ({check.detail})")

        if check.passed:
            # A stepped-down candidate (attempt > 1) is BELOW the scanner's
            # recommendation, so it may not actually fix the advisory: open it as
            # a draft for a human to confirm (config-gated). A major bump never
            # auto-ships either, per the same policy rule as the primary path.
            stepped_down = attempt > 1
            forced_major = (classify(declared, candidate) == Delta.MAJOR
                            and not cfg.allow_major_auto)
            below_rec = stepped_down and cfg.draft_below_recommended
            cand_draft = draft or forced_major or below_rec
            labels = list(decision.labels)
            if forced_major and not draft:
                labels += ["needs-human", "major-bump"]
            if below_rec:
                labels += ["needs-human", "below-recommended"]
            res.to_version = candidate
            body = render_pr_body(
                finding, declared, candidate, check.detail,
                llm.write_pr_description(finding, declared, candidate, check.detail),
                cand_draft, labels,
            )
            info = pr.open_pr(
                repo_dir, base_branch, finding, declared, candidate, body,
                commit_msg=f"fix(deps): bump {finding.package} {declared} -> {candidate}",
                draft=cand_draft,
            )
            res.branch = info["branch"]
            res.pr_path = info["pr_path"]
            res.pr_url = info["pr_url"]
            res.outcome = Outcome.ESCALATED if cand_draft else Outcome.FIXED
            if stepped_down:
                via = (f" (stepped down to {candidate}, below recommended "
                       f"{finding.recommended_version})")
            else:
                via = ""
            if cand_draft:
                why = ("below recommended -- needs human confirmation" if below_rec
                       else "major bump") if not draft else "policy draft"
                res.reason = f"draft PR opened for human review ({why}){via}"
            else:
                res.reason = "checks passed; PR opened" + via
            res.log(f"done: {'draft ' if cand_draft else ''}PR on branch "
                    f"{info['branch']}{via}")
            return res

        res.log(f"attempt {attempt} failed ({candidate}); restoring declared version")
        edit.restore()  # back to the declared version before the next candidate

    # every candidate failed -> stay on the declared version + attach a comment
    res.outcome = Outcome.ESCALATED
    res.reason = (f"bump failed validation after {res.attempts} attempt(s) "
                  f"({', '.join(candidates) or 'no valid candidate'}); "
                  f"restored to {declared}")
    if last_check is not None:
        res.reason += f": {last_check.detail}"
    note = pr.write_comment(repo_dir, finding, declared, candidates, last_check, res.audit)
    res.pr_path = note
    res.log("escalate + comment: " + res.reason)
    return res
