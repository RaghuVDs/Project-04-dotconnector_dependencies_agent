"""Policy: the rules that decide what the agent is allowed to do automatically.

Deterministic and config-driven on purpose -- you want this auditable and the
same every run, not an LLM whim. The LLM operates *within* the band this allows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Finding
from .tools.semver_tool import Delta


class Action(str, Enum):
    AUTO = "auto"               # apply + validate + normal PR
    DRAFT_HUMAN = "draft_human"  # apply + validate + DRAFT PR, needs-human
    ESCALATE = "escalate"       # don't change; flag for a human
    SKIP = "skip"               # waived / nothing to do


@dataclass
class PolicyDecision:
    action: Action
    reason: str
    labels: list[str] = field(default_factory=list)


@dataclass
class PolicyConfig:
    auto_deltas: tuple[Delta, ...] = (Delta.PATCH, Delta.MINOR)
    allow_major_auto: bool = False
    max_attempts: int = 3          # bounded self-healing retries
    max_prs_per_run: int = 10      # blast-radius cap
    max_llm_calls_per_finding: int = 6
    # self-healing fallback: if the recommended version fails validation, step
    # DOWN the version ladder (previous release, then the one before it, ...)
    # before giving up. Off => try only the recommended version, then revert.
    fallback_to_previous: bool = True
    # a stepped-down version is BELOW the scanner's recommendation, so it may not
    # actually remediate the advisory: open it as a draft for a human to confirm
    # rather than auto-merging. Set false to auto-ship stepped-down versions.
    draft_below_recommended: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyConfig":
        deltas = d.get("auto_deltas")
        kwargs: dict = {}
        if deltas:
            kwargs["auto_deltas"] = tuple(Delta(x) for x in deltas)
        for key in ("allow_major_auto", "max_attempts", "max_prs_per_run",
                    "max_llm_calls_per_finding", "fallback_to_previous",
                    "draft_below_recommended"):
            if key in d:
                kwargs[key] = d[key]
        return cls(**kwargs)


class Resolver(str, Enum):
    """How `version_resolvable` is checked."""
    REGISTRY = "registry"          # fast proxy: every '==' pin must exist on PyPI
    PIP_DRY_RUN = "pip_dry_run"    # real resolver: the dependency graph must resolve


class Isolation(str, Enum):
    """Where the tests run."""
    NONE = "none"                  # in the agent's own environment (no install)
    VENV = "venv"                  # in a throwaway venv with the bumped deps installed


@dataclass
class ValidationConfig:
    """How the validation gate proves a bump is safe.

    Defaults are 'real' (resolve the graph + run tests against the installed
    bump). Set resolver=registry + isolation=none to reproduce the fast,
    fully-offline behavior with no pip/venv subprocess.
    """
    resolver: Resolver = Resolver.PIP_DRY_RUN
    isolation: Isolation = Isolation.VENV
    test_command: tuple[str, ...] = ("{python}", "-m", "pytest", "-q")
    resolve_timeout: int = 120
    test_timeout: int = 300
    reuse_venv: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationConfig":
        kwargs: dict = {}
        if "resolver" in d:
            kwargs["resolver"] = Resolver(d["resolver"])   # bad value -> ValueError (fail fast)
        if "isolation" in d:
            kwargs["isolation"] = Isolation(d["isolation"])
        if d.get("test_command"):
            kwargs["test_command"] = tuple(d["test_command"])
        for key in ("resolve_timeout", "test_timeout", "reuse_venv"):
            if key in d:
                kwargs[key] = d[key]
        return cls(**kwargs)


def decide(finding: Finding, delta: Delta, cfg: PolicyConfig) -> PolicyDecision:
    if finding.waived:
        return PolicyDecision(Action.SKIP, "finding is waived")
    if not finding.recommended_version:
        return PolicyDecision(Action.ESCALATE, "no recommended version provided",
                              ["needs-human", "no-recommendation"])
    if delta in (Delta.DOWNGRADE, Delta.SAME, Delta.UNKNOWN):
        return PolicyDecision(
            Action.ESCALATE,
            f"recommended version is not a clean upgrade (delta={delta.value})",
            ["needs-human"],
        )
    if delta == Delta.MAJOR and not cfg.allow_major_auto:
        return PolicyDecision(
            Action.DRAFT_HUMAN,
            "major version bump -- opening a draft for human review",
            ["needs-human", "major-bump"],
        )
    if delta in cfg.auto_deltas or (delta == Delta.MAJOR and cfg.allow_major_auto):
        return PolicyDecision(Action.AUTO, f"{delta.value} bump within auto policy",
                              ["automated", "dependencies"])
    return PolicyDecision(Action.ESCALATE, f"delta {delta.value} not in auto policy",
                          ["needs-human"])
