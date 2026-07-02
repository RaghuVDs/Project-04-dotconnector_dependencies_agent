"""Unit: validator helper functions and VenvSession. subprocess is mocked."""
import subprocess

import pytest

from src.models import Ecosystem
from src.tools import validator as v

pytestmark = pytest.mark.unit

from helpers import FakeRegistry  # noqa: E402


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRun:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result or _Proc(0)
        self.raises = raises

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        if self.raises is not None:
            raise self.raises
        return self.result


# ---- tiny helpers -----------------------------------------------------------
def test_tail_truncates():
    assert v._tail("abcdef", 3) == "def"
    assert v._tail("ab", 5) == "ab"


def test_interpreter_path_crossplatform():
    win = v._interpreter_path(v.Path("root"), windows=True)
    nix = v._interpreter_path(v.Path("root"), windows=False)
    assert win.endswith("Scripts" + __import__("os").sep + "python.exe")
    assert nix.endswith("bin" + __import__("os").sep + "python")


# ---- manifest_parses --------------------------------------------------------
def test_manifest_parses_ok(tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.1.3\n# comment\n-r other.txt\n\n", encoding="utf-8")
    ok, msg = v._check_manifest_parses(p)
    assert ok is True and "parses" in msg


def test_manifest_parses_rejects_unparseable_pin(tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("this is == broken ==\n", encoding="utf-8")
    ok, _ = v._check_manifest_parses(p)
    assert ok is False


def test_manifest_parses_unreadable_file(tmp_path):
    ok, msg = v._check_manifest_parses(tmp_path / "missing.txt")
    assert ok is False and "cannot read" in msg


# ---- registry existence prefilter ------------------------------------------
def test_registry_existence_all_present(tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.1.3\nrequests==2.33.1\n", encoding="utf-8")
    reg = FakeRegistry(["3.1.3", "2.33.1"])
    ok, msg = v._check_registry_existence(p, Ecosystem.PYPI, reg)
    assert ok is True


def test_registry_existence_reports_missing(tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==9.9.9\n", encoding="utf-8")
    ok, msg = v._check_registry_existence(p, Ecosystem.PYPI, FakeRegistry([]))
    assert ok is False and "9.9.9" in msg


def test_registry_existence_skips_star_and_non_pins(tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.*\nrequests>=2.0\n", encoding="utf-8")
    # star pin and non-'==' op are skipped -> nothing to check -> ok
    ok, _ = v._check_registry_existence(p, Ecosystem.PYPI, FakeRegistry([]))
    assert ok is True


# ---- pip resolves -----------------------------------------------------------
def test_pip_resolves_ok(monkeypatch, tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.1.3\n", encoding="utf-8")
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(0, stdout="Would install")))
    ok, detail, _ = v._check_pip_resolves(p, None, 10)
    assert ok is True and "resolves" in detail


def test_pip_resolves_conflict(monkeypatch, tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.1.3\n", encoding="utf-8")
    monkeypatch.setattr(v.subprocess, "run",
                        _FakeRun(_Proc(1, stderr="ResolutionImpossible")))
    ok, detail, output = v._check_pip_resolves(p, None, 10)
    assert ok is False and "ResolutionImpossible" in detail and "ResolutionImpossible" in output


def test_pip_resolves_timeout(monkeypatch, tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.1.3\n", encoding="utf-8")
    monkeypatch.setattr(v.subprocess, "run",
                        _FakeRun(raises=subprocess.TimeoutExpired("pip", 10)))
    ok, detail, _ = v._check_pip_resolves(p, None, 10)
    assert ok is False and "timed out" in detail


def test_pip_resolves_pip_missing(monkeypatch, tmp_path):
    p = tmp_path / "requirements.txt"
    p.write_text("flask==3.1.3\n", encoding="utf-8")
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(raises=FileNotFoundError("no pip")))
    ok, detail, _ = v._check_pip_resolves(p, None, 10)
    assert ok is False and "pip not available" in detail


# ---- tests -----------------------------------------------------------------
def test_check_tests_no_command_skips(tmp_path):
    ok, msg, _ = v._check_tests(tmp_path, [], 10)
    assert ok is True and "skipped" in msg


def test_check_tests_pass_and_substitutes_python(monkeypatch, tmp_path):
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(v.subprocess, "run", fake)
    ok, _, _ = v._check_tests(tmp_path, ["{python}", "-m", "pytest"], 10,
                              python_exe="/venv/bin/python")
    assert ok is True and fake.calls[0][0] == "/venv/bin/python"


def test_check_tests_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(1, stdout="F")))
    ok, msg, _ = v._check_tests(tmp_path, ["pytest"], 10)
    assert ok is False and "exit 1" in msg


def test_check_tests_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(v.subprocess, "run",
                        _FakeRun(raises=subprocess.TimeoutExpired("pytest", 10)))
    ok, msg, _ = v._check_tests(tmp_path, ["pytest"], 10)
    assert ok is False and "timed out" in msg


def test_check_tests_command_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(raises=FileNotFoundError("x")))
    ok, msg, _ = v._check_tests(tmp_path, ["nope"], 10)
    assert ok is False and "not found" in msg


# ---- VenvSession ------------------------------------------------------------
def test_make_venv_session_and_python_path(tmp_path):
    s = v.make_venv_session(tmp_path / "venv", reuse=True)
    assert s.python.endswith("python.exe") or s.python.endswith("python")


def test_venv_ensure_created_reuses_existing(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv")
    # fabricate the interpreter so reuse short-circuits before any subprocess
    interp = v.Path(s.python)
    interp.parent.mkdir(parents=True, exist_ok=True)
    interp.write_text("", encoding="utf-8")
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(v.subprocess, "run", fake)
    s.ensure_created(10)
    assert fake.calls == []     # reused -> venv never invoked


def test_venv_ensure_created_creates(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv", reuse=False)
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(v.subprocess, "run", fake)
    s.ensure_created(10)
    assert any("venv" in part for call in fake.calls for part in call)


def test_venv_ensure_created_failure_raises(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv", reuse=False)
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(1, stderr="nope")))
    with pytest.raises(v.VenvCreateError):
        s.ensure_created(10)


def test_venv_ensure_created_timeout_raises(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv", reuse=False)
    monkeypatch.setattr(v.subprocess, "run",
                        _FakeRun(raises=subprocess.TimeoutExpired("venv", 1)))
    with pytest.raises(v.VenvCreateError):
        s.ensure_created(1)


def test_venv_install_success(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv")
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(0, stdout="installed")))
    ok, out = s.install(tmp_path / "requirements.txt", 10)
    assert ok is True and "installed" in out


def test_venv_install_conflict(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv")
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(_Proc(1, stderr="conflict")))
    ok, out = s.install(tmp_path / "requirements.txt", 10)
    assert ok is False and "conflict" in out


def test_venv_install_timeout(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv")
    monkeypatch.setattr(v.subprocess, "run",
                        _FakeRun(raises=subprocess.TimeoutExpired("pip", 10)))
    ok, out = s.install(tmp_path / "requirements.txt", 10)
    assert ok is False and "timed out" in out


def test_venv_install_pip_missing(monkeypatch, tmp_path):
    s = v.make_venv_session(tmp_path / "venv")
    monkeypatch.setattr(v.subprocess, "run", _FakeRun(raises=FileNotFoundError("x")))
    ok, out = s.install(tmp_path / "requirements.txt", 10)
    assert ok is False and "pip not available" in out
