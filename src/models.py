"""Canonical data models shared across the pipeline.

A `Finding` is the normalized form of one DotConnector dependency issue.
Everything downstream (triage, agent, tools, PR) speaks in `Finding`s and
`RemediationResult`s so the rest of the system never has to care what the
upstream scanner's payload looked like.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"
    NONE = "none"


class Ecosystem(str, Enum):
    PYPI = "pypi"
    MAVEN = "maven"
    NPM = "npm"
    CARGO = "cargo"
    GO = "go"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    FIXED = "fixed"             # edit applied, checks passed, PR opened
    ESCALATED = "escalated"     # opened as draft / needs a human
    SKIPPED = "skipped"         # waived or nothing to do
    ERROR = "error"             # unexpected failure in the loop


class Finding(BaseModel):
    """One vulnerable-dependency finding from DotConnector."""

    finding_id: str
    repository: str
    ecosystem: Ecosystem
    package: str
    declared_version: str
    recommended_version: Optional[str] = None
    severity: Severity = Severity.UNKNOWN
    cve_ids: list[str] = Field(default_factory=list)
    file_path: Optional[str] = None      # hint only; we re-locate to be safe
    namespace: Optional[str] = None      # e.g. maven groupId
    purl: Optional[str] = None           # package URL, the cross-ecosystem key
    waived: bool = False

    @property
    def fingerprint(self) -> str:
        """Stable identity for idempotency / dedup.

        Two runs that would make the same change to the same repo produce the
        same fingerprint, so we never open duplicate PRs.
        """
        target = self.recommended_version or "none"
        return f"{self.repository}::{self.purl or self.package}::{target}"


class CheckResult(BaseModel):
    """Result of the validation gate (the 'passing check')."""

    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    detail: str = ""
    test_output: str = ""

    @classmethod
    def failure(cls, name: str, detail: str, output: str = "") -> "CheckResult":
        return cls(passed=False, checks={name: False}, detail=detail, test_output=output)


class RemediationResult(BaseModel):
    """What the agent did with a single finding."""

    finding_id: str
    package: str
    outcome: Outcome
    from_version: str
    to_version: Optional[str] = None
    attempts: int = 0
    reason: str = ""
    branch: Optional[str] = None
    pr_path: Optional[str] = None      # local PR body (POC) or PR url (prod)
    pr_url: Optional[str] = None
    audit: list[str] = Field(default_factory=list)   # human-readable decision log

    def log(self, msg: str) -> None:
        self.audit.append(msg)
