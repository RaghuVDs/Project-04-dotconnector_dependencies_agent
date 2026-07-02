"""Functional: validate() end-to-end across every config branch. The only
externals are subprocess (mocked) and a fake venv/registry."""
import pytest

from src.models import Ecosystem
from src.policy import Isolation, Resolver, ValidationConfig
from src.tools import validator as v

pytestmark = pytest.mark.functional

from helpers import FakeRegistry  # noqa: E402


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _FakeRun:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        return self.result


class _FakeVenv:
    def __init__(self, install_ok=True, out="installed deps"):
        self.install_ok, self.out, self.python = install_ok, out, "/venv/python"

    def install(self, manifest_path, timeout):
        return self.install_ok, self.out


def _req(tmp_path, body="flask==3.1.3\n"):
    p = tmp_path / "requirements.txt"
    p.write_text(body, encoding="utf-8")
    return p


def test_manifest_parse_failure_short_circuits(tmp_path):
    p = _req(tmp_path, "garbage == == broken\n")
    cfg = ValidationConfig(resolver=Resolver.REGISTRY, isolation=Isolation.NONE)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg)
    assert res.passed is False and res.checks["manifest_parses"] is False


def test_registry_prefilter_failure_short_circuits(monkeypatch, tmp_path):
    p = _req(tmp_path, "flask==9.9.9\n")
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(v.subprocess, "run", fake)
    cfg = ValidationConfig(resolver=Resolver.PIP_DRY_RUN, isolation=Isolation.NONE)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry([]), cfg)
    assert res.passed is False and res.checks["version_resolvable"] is False
    assert fake.calls == []   # never reached pip


def test_venv_mode_install_then_tests_pass(monkeypatch, tmp_path):
    p = _req(tmp_path)
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(v.subprocess, "run", fake)
    cfg = ValidationConfig(resolver=Resolver.PIP_DRY_RUN, isolation=Isolation.VENV,
                           test_command=("{python}", "-m", "pytest"))
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg,
                     venv=_FakeVenv(install_ok=True))
    assert res.passed is True
    assert res.checks == {"manifest_parses": True, "version_resolvable": True,
                          "tests_pass": True}
    # tests ran with the venv interpreter
    assert fake.calls[0][0] == "/venv/python"


def test_venv_mode_install_conflict_fails(tmp_path):
    p = _req(tmp_path)
    cfg = ValidationConfig(resolver=Resolver.PIP_DRY_RUN, isolation=Isolation.VENV)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg,
                     venv=_FakeVenv(install_ok=False, out="ResolutionImpossible"))
    assert res.passed is False and res.checks["version_resolvable"] is False
    assert "ResolutionImpossible" in res.test_output


def test_pip_dry_run_mode_resolves_then_tests(monkeypatch, tmp_path):
    p = _req(tmp_path)
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(0)))
    cfg = ValidationConfig(resolver=Resolver.PIP_DRY_RUN, isolation=Isolation.NONE)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg)
    assert res.passed is True and res.checks["version_resolvable"] is True


def test_pip_dry_run_conflict_fails(monkeypatch, tmp_path):
    p = _req(tmp_path)
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(1, stderr="ResolutionImpossible")))
    cfg = ValidationConfig(resolver=Resolver.PIP_DRY_RUN, isolation=Isolation.NONE)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg)
    assert res.passed is False and res.checks["version_resolvable"] is False


def test_registry_only_mode_no_pip(monkeypatch, tmp_path):
    p = _req(tmp_path)
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(v.subprocess, "run", fake)
    cfg = ValidationConfig(resolver=Resolver.REGISTRY, isolation=Isolation.NONE)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg)
    assert res.passed is True
    # the only subprocess call is the test command -- pip was never invoked
    assert len(fake.calls) == 1
    assert not any("pip" in part for call in fake.calls for part in call)


def test_tests_failure_makes_overall_fail(monkeypatch, tmp_path):
    p = _req(tmp_path)
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(1, stdout="1 failed")))
    cfg = ValidationConfig(resolver=Resolver.REGISTRY, isolation=Isolation.NONE)
    res = v.validate(tmp_path, p, Ecosystem.PYPI, FakeRegistry(["3.1.3"]), cfg)
    assert res.passed is False and res.checks["tests_pass"] is False
