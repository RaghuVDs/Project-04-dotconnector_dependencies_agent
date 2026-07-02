"""Unit: PyPI registry client. The network call is mocked; the lru_cache is
cleared between tests so monkeypatched payloads never leak."""
import io
import json
import urllib.request

import pytest

from src.models import Ecosystem
from src.tools import registry as reg
from src.tools.registry import Registry, RegistryError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    reg._pypi_payload.cache_clear()
    yield
    reg._pypi_payload.cache_clear()


def _mock_urlopen(monkeypatch, payload=None, exc=None):
    def fake(url, timeout=0):
        if exc is not None:
            raise exc
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _releases(*versions, include_empty=False, include_bad=False):
    rel = {v: [{"filename": f"x-{v}.whl"}] for v in versions}
    if include_empty:
        rel["9.9.9"] = []          # yanked / no files -> skipped
    if include_bad:
        rel["not-a-version"] = [{"filename": "x.whl"}]  # unparseable -> skipped
    return {"releases": rel}


def test_pypi_payload_success(monkeypatch):
    _mock_urlopen(monkeypatch, _releases("1.0.0"))
    assert "releases" in reg._pypi_payload("flask")


def test_pypi_payload_network_error_wrapped(monkeypatch):
    _mock_urlopen(monkeypatch, exc=OSError("no network"))
    with pytest.raises(RegistryError):
        reg._pypi_payload("flask")


def test_list_versions_filters_and_sorts(monkeypatch):
    _mock_urlopen(monkeypatch,
                  _releases("1.10.0", "1.2.0", "1.0.0", include_empty=True, include_bad=True))
    out = Registry().list_versions(Ecosystem.PYPI, "flask")
    assert out == ["1.0.0", "1.2.0", "1.10.0"]   # sorted by Version, junk dropped


def test_list_versions_non_pypi_raises(monkeypatch):
    with pytest.raises(RegistryError):
        Registry().list_versions(Ecosystem.MAVEN, "spring-core")


def test_version_exists_true_false(monkeypatch):
    _mock_urlopen(monkeypatch, _releases("1.0.0", "1.1.0"))
    r = Registry()
    assert r.version_exists(Ecosystem.PYPI, "flask", "1.1.0") is True
    assert r.version_exists(Ecosystem.PYPI, "flask", "2.0.0") is False


def test_version_exists_swallows_registry_error(monkeypatch):
    _mock_urlopen(monkeypatch, exc=OSError("down"))
    assert Registry().version_exists(Ecosystem.PYPI, "flask", "1.0.0") is False


def test_latest_version(monkeypatch):
    _mock_urlopen(monkeypatch, _releases("1.0.0", "2.3.4", "2.0.0"))
    assert Registry().latest_version(Ecosystem.PYPI, "flask") == "2.3.4"


def test_latest_version_empty_is_none(monkeypatch):
    _mock_urlopen(monkeypatch, {"releases": {}})
    assert Registry().latest_version(Ecosystem.PYPI, "flask") is None


def test_latest_patch_within_minor(monkeypatch):
    _mock_urlopen(monkeypatch, _releases("3.1.0", "3.1.2", "3.1.5", "3.2.0"))
    assert Registry().latest_patch_within_minor(Ecosystem.PYPI, "flask", "3.1.1") == "3.1.5"


def test_latest_patch_within_minor_bad_base_is_none(monkeypatch):
    _mock_urlopen(monkeypatch, _releases("3.1.0"))
    assert Registry().latest_patch_within_minor(Ecosystem.PYPI, "flask", "junk") is None


def test_latest_patch_within_minor_none_in_line(monkeypatch):
    _mock_urlopen(monkeypatch, _releases("3.1.0", "3.1.2"))
    # base 3.2.0 has no >= sibling in the 3.2 line
    assert Registry().latest_patch_within_minor(Ecosystem.PYPI, "flask", "3.2.0") is None
