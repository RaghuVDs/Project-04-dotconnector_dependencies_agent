"""The LLM layer -- the agent's *judgment*.

Three judgment calls are delegated to the model:
  * plan_fix      - is the recommended version a sensible target here?
  * diagnose      - a check failed; what should we try next (or give up)?
  * write_pr_desc - explain the change for the human reviewer.

Everything else (locating files, comparing versions, editing, validating,
opening PRs) is deterministic tooling. The model is kept on a short leash:
strict JSON in/out, a per-finding call cap, and it can only ever *propose* a
version -- the registry and the validator decide if that proposal is real.

Provider-agnostic by design. For dev we default to a FREE provider (Groq's
OpenAI-compatible endpoint, no credit card). Swap to Anthropic / the Claude
Agent SDK for prod by changing config -- the agent code doesn't change.
If no key is configured it falls back to a deterministic StubLLM so the whole
pipeline still runs offline.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from .models import CheckResult, Ecosystem, Finding
from .tools.registry import Registry
from .tools.semver_tool import Delta

# provider -> (base_url, default_model, api_key_env). All OpenAI-compatible.
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/",
               "gemini-2.0-flash", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1",
                   "meta-llama/llama-3.3-70b-instruct:free", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "llama3.1", "OLLAMA_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
}


@dataclass
class FixPlan:
    target_version: str
    auto_fixable: bool
    reasoning: str


@dataclass
class Diagnosis:
    give_up: bool
    next_version: str | None
    reasoning: str


class LLM(Protocol):  # pragma: no cover - typing-only interface, no runtime body
    def plan_fix(self, finding: Finding, delta: Delta,
                 manifest_excerpt: str, available_versions: list[str]) -> FixPlan: ...
    def diagnose(self, finding: Finding, attempted: str,
                 check: CheckResult, available_versions: list[str]) -> Diagnosis: ...
    def write_pr_description(self, finding: Finding, from_v: str,
                             to_v: str, check_detail: str) -> str: ...
    def reset_budget(self) -> None:
        """Reset the per-finding call counter. Called once per finding so the
        `max_llm_calls_per_finding` cap is genuinely per-finding, not per-run."""
        ...


# --------------------------------------------------------------------------- #
# Deterministic fallback (no network) -- keeps the POC runnable everywhere.
# --------------------------------------------------------------------------- #
class StubLLM:
    def __init__(self, registry: Registry | None = None):
        self.registry = registry
        self.calls = 0

    def reset_budget(self) -> None:
        self.calls = 0

    def plan_fix(self, finding, delta, manifest_excerpt, available_versions):
        self.calls += 1
        target = finding.recommended_version or ""
        return FixPlan(
            target_version=target,
            auto_fixable=bool(target),
            reasoning=f"[stub] take recommended {target} ({delta.value} bump).",
        )

    def diagnose(self, finding, attempted, check, available_versions):
        self.calls += 1
        # one deterministic retry: try the highest patch in the same minor line
        if self.registry and finding.recommended_version:
            alt = self.registry.latest_patch_within_minor(
                finding.ecosystem, finding.package, finding.recommended_version
            )
            if alt and alt != attempted:
                return Diagnosis(False, alt, f"[stub] retry with {alt}.")
        return Diagnosis(True, None, "[stub] no further heuristic; escalate.")

    def write_pr_description(self, finding, from_v, to_v, check_detail):
        self.calls += 1
        cves = ", ".join(finding.cve_ids) if finding.cve_ids else "the reported advisory"
        return (f"Updates `{finding.package}` from {from_v} to {to_v} to remediate "
                f"{cves} (severity: {finding.severity.value}). Validation: {check_detail}.")


# --------------------------------------------------------------------------- #
# Real provider (any OpenAI-compatible endpoint, incl. free Groq/Gemini).
# --------------------------------------------------------------------------- #
class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, max_calls: int = 6):
        from openai import OpenAI  # imported lazily so stub mode needs no openai
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.max_calls = max_calls
        self.calls = 0

    def reset_budget(self) -> None:
        self.calls = 0

    def _chat_json(self, system: str, user: str) -> dict:
        if self.calls >= self.max_calls:
            raise RuntimeError("LLM call budget exhausted for this finding")
        self.calls += 1
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0,
                response_format={"type": "json_object"},
            )
        except Exception:  # provider may not support response_format; retry plain
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0,
            )
        return _parse_json(resp.choices[0].message.content or "{}")

    def plan_fix(self, finding, delta, manifest_excerpt, available_versions):
        system = (
            "You are a dependency-remediation engineer. Decide whether the "
            "recommended version is a safe, valid target for an AUTOMATED bump. "
            "Only propose a version that appears in available_versions. "
            'Respond with strict JSON: {"target_version": str, '
            '"auto_fixable": bool, "reasoning": str}.'
        )
        user = json.dumps({
            "package": finding.package, "ecosystem": finding.ecosystem.value,
            "declared_version": finding.declared_version,
            "recommended_version": finding.recommended_version,
            "bump_delta": delta.value, "severity": finding.severity.value,
            "manifest_excerpt": manifest_excerpt,
            "available_versions_tail": available_versions[-25:],
        })
        d = self._chat_json(system, user)
        return FixPlan(
            target_version=str(d.get("target_version") or finding.recommended_version or ""),
            auto_fixable=bool(d.get("auto_fixable", False)),
            reasoning=str(d.get("reasoning", "")),
        )

    def diagnose(self, finding, attempted, check, available_versions):
        system = (
            "A dependency bump failed validation. Propose the next version to try "
            "from available_versions, or give up. Prefer the smallest change that "
            "could fix the failure. Respond with strict JSON: "
            '{"give_up": bool, "next_version": str|null, "reasoning": str}.'
        )
        user = json.dumps({
            "package": finding.package, "attempted_version": attempted,
            "failed_checks": {k: v for k, v in check.checks.items() if not v},
            "detail": check.detail,
            "test_output_tail": check.test_output[-1500:],
            "available_versions_tail": available_versions[-25:],
        })
        d = self._chat_json(system, user)
        nv = d.get("next_version")
        return Diagnosis(
            give_up=bool(d.get("give_up", True)),
            next_version=str(nv) if nv else None,
            reasoning=str(d.get("reasoning", "")),
        )

    def write_pr_description(self, finding, from_v, to_v, check_detail):
        system = ("Write a concise, factual PR description (2-4 sentences) for a "
                  "dependency security bump. No marketing. Plain prose.")
        user = json.dumps({
            "package": finding.package, "from": from_v, "to": to_v,
            "severity": finding.severity.value, "cves": finding.cve_ids,
            "validation": check_detail,
        })
        try:
            self.calls += 1
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.2,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            return f"Bump {finding.package} {from_v} -> {to_v}. ({exc})"


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def make_llm(registry: Registry, max_calls: int = 6) -> tuple[LLM, str]:
    """Build an LLM from env. Returns (llm, label). Falls back to stub."""
    provider = os.environ.get("LLM_PROVIDER", "stub").lower()
    if provider in ("", "stub", "none"):
        return StubLLM(registry), "stub (offline, deterministic)"
    if provider not in PROVIDERS:
        return StubLLM(registry), f"stub (unknown provider {provider!r})"

    base_url, default_model, key_env = PROVIDERS[provider]
    base_url = os.environ.get("LLM_BASE_URL", base_url)
    model = os.environ.get("LLM_MODEL", default_model)
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get(key_env)
    if not api_key and provider != "ollama":
        return StubLLM(registry), f"stub (no API key for {provider}; set {key_env})"
    try:
        return OpenAICompatLLM(base_url, api_key or "ollama", model, max_calls), \
            f"{provider}:{model}"
    except Exception as exc:  # openai not installed, etc.
        return StubLLM(registry), f"stub (could not init {provider}: {exc})"
