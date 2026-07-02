"""Unit: manifest location + format-preserving, reversible editing."""
import sys
import types

import pytest

from src.models import Ecosystem
from src.tools import manifest as m

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("raw,expected", [
    ("Flask", "flask"), ("pytest_runner", "pytest-runner"),
    ("a.b_c", "a-b-c"), ("ALREADY-norm", "already-norm"),
])
def test_normalize(raw, expected):
    assert m.normalize(raw) == expected


def test_find_manifests_unsupported_ecosystem_is_empty(tmp_path):
    assert m.find_manifests(tmp_path, Ecosystem.MAVEN) == []


def test_find_manifests_dedupes_overlapping_globs(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    found = m.find_manifests(tmp_path, Ecosystem.PYPI)
    # 'requirements*.txt' and '**/requirements*.txt' both match the top-level
    # file -> must be de-duped to one entry
    assert found.count(tmp_path / "requirements.txt") == 1


def test_locate_requirements_and_normalization(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask==3.0.0\nRequests==2.31.0  # http\n", encoding="utf-8")
    assert m.locate(tmp_path, Ecosystem.PYPI, "flask") == (req, "3.0.0")
    # 'requests' matches 'Requests'
    assert m.locate(tmp_path, Ecosystem.PYPI, "requests")[1] == "2.31.0"


def test_locate_returns_none_when_absent(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    assert m.locate(tmp_path, Ecosystem.PYPI, "django") is None


def test_excerpt_returns_context_window(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("a==1\nb==2\nflask==3.0.0\nc==4\nd==5\n", encoding="utf-8")
    ex = m.excerpt(req, "flask", context=1)
    assert ex.splitlines() == ["b==2", "flask==3.0.0", "c==4"]


def test_excerpt_missing_returns_empty(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("a==1\n", encoding="utf-8")
    assert m.excerpt(req, "flask") == ""


@pytest.mark.parametrize("line,must_contain", [
    ("flask==3.0.0", "flask==3.1.3"),
    ("Flask==3.0.0  # web", "# web"),                      # case + comment kept
    ("flask[async]==3.0.0", "flask[async]==3.1.3"),        # extras kept
    ("flask==3.0.0 ; python_version>='3.8'", "; python_version>='3.8'"),  # markers kept
])
def test_set_version_preserves_style(tmp_path, line, must_contain):
    # set_version always pins with '==' but keeps spacing/extras/markers/comments.
    req = tmp_path / "requirements.txt"
    req.write_text(line + "\n", encoding="utf-8")
    edit = m.set_version(req, "flask", "3.1.3")
    text = req.read_text()
    assert "3.1.3" in text and must_contain in text
    assert edit.new_version == "3.1.3"


def test_set_version_rewrites_operator_to_double_equals(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=3.0.0\n", encoding="utf-8")
    m.set_version(req, "flask", "3.1.3")
    assert "flask>=" not in req.read_text()
    assert "==3.1.3" in req.read_text()


def test_set_version_only_changes_first_match(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask==3.0.0\nflask==3.0.0\n", encoding="utf-8")
    m.set_version(req, "flask", "3.1.3")
    assert req.read_text().count("3.1.3") == 1


def test_set_version_missing_raises(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask==3.0.0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        m.set_version(req, "django", "5.0")


def test_set_version_records_old_and_restores(tmp_path):
    req = tmp_path / "requirements.txt"
    original = "flask==3.0.0\nrequests==2.31.0\n"
    req.write_text(original, encoding="utf-8")
    edit = m.set_version(req, "flask", "3.1.3")
    assert edit.old_version == "3.0.0"
    assert "flask==3.1.3" in req.read_text()
    edit.restore()
    assert req.read_text() == original


def test_set_version_preserves_no_trailing_newline(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask==3.0.0", encoding="utf-8")  # no trailing \n
    m.set_version(req, "flask", "3.1.3")
    assert req.read_text() == "flask==3.1.3"


# ---- pyproject.toml path ----------------------------------------------------
def test_declared_in_pyproject_without_tomllib_returns_none(tmp_path, monkeypatch):
    # On Python < 3.11 tomllib is absent -> the function returns None gracefully.
    # Force a ModuleNotFoundError for 'tomllib' regardless of the host version.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\ndependencies = ["flask==3.0.0"]\n', encoding="utf-8")
    assert m._declared_in_pyproject(py, "flask") is None


def test_declared_in_pyproject_with_tomllib(tmp_path, monkeypatch):
    fake = types.ModuleType("tomllib")
    fake.loads = lambda text: {"project": {"dependencies": ["flask==3.0.0", "x==1"]}}
    monkeypatch.setitem(sys.modules, "tomllib", fake)
    py = tmp_path / "pyproject.toml"
    py.write_text("ignored", encoding="utf-8")
    assert m._declared_in_pyproject(py, "flask") == "3.0.0"
    assert m._declared_in_pyproject(py, "absent") is None


def test_locate_uses_pyproject_when_present(tmp_path, monkeypatch):
    fake = types.ModuleType("tomllib")
    fake.loads = lambda text: {"project": {"dependencies": ["flask==3.0.0"]}}
    monkeypatch.setitem(sys.modules, "tomllib", fake)
    (tmp_path / "pyproject.toml").write_text("x", encoding="utf-8")
    located = m.locate(tmp_path, Ecosystem.PYPI, "flask")
    assert located is not None and located[1] == "3.0.0"
