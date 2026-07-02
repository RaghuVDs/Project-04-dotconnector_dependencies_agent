"""Unit: version math (semver_tool). Pure, deterministic."""
import pytest

from src.tools.semver_tool import (
    Delta, classify, is_upgrade, parse, previous_versions,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("declared,recommended,expected", [
    ("3.0.0", "3.1.3", Delta.MINOR),
    ("8.2.0", "9.0.3", Delta.MAJOR),
    ("2.31.0", "2.31.4", Delta.PATCH),
    ("1.0.0", "1.0.0", Delta.SAME),
    ("1.2", "1.2.0", Delta.SAME),           # _triple pads missing components
    ("2.0.0", "1.9.0", Delta.DOWNGRADE),
    ("1", "2", Delta.MAJOR),                # single-component versions
    ("not-a-version", "1.0.0", Delta.UNKNOWN),
    ("1.0.0", "garbage", Delta.UNKNOWN),
])
def test_classify(declared, recommended, expected):
    assert classify(declared, recommended) == expected


def test_parse_valid_invalid_and_none():
    assert parse("1.2.3") is not None
    assert parse("nope") is None
    assert parse(None) is None  # TypeError swallowed


def test_is_upgrade():
    assert is_upgrade("1.0.0", "1.0.1") is True
    assert is_upgrade("1.0.1", "1.0.0") is False
    assert is_upgrade("1.0.0", "1.0.0") is False
    assert is_upgrade("bad", "1.0.0") is False


class TestPreviousVersions:
    VERSIONS = ["3.0.0", "3.1.0", "3.1.1", "3.1.2", "3.1.3", "3.2.0"]

    def test_steps_down_highest_first(self):
        assert previous_versions(self.VERSIONS, "3.1.3", floor="3.0.0", count=2) \
            == ["3.1.2", "3.1.1"]

    def test_count_caps_results(self):
        assert previous_versions(self.VERSIONS, "3.1.3", floor="3.0.0", count=1) \
            == ["3.1.2"]

    def test_floor_is_exclusive(self):
        out = previous_versions(self.VERSIONS, "3.1.3", floor="3.0.0", count=99)
        assert "3.0.0" not in out
        assert out == ["3.1.2", "3.1.1", "3.1.0"]

    def test_nothing_below_floor_is_empty(self):
        assert previous_versions(self.VERSIONS, "3.1.0", floor="3.0.9", count=3) == []

    def test_no_floor_returns_all_below(self):
        assert previous_versions(self.VERSIONS, "3.1.1", count=99) \
            == ["3.1.0", "3.0.0"]

    def test_unparseable_current_returns_empty(self):
        assert previous_versions(self.VERSIONS, "not-a-version") == []

    def test_unparseable_floor_is_ignored(self):
        # a bad floor parses to None -> treated as "no floor"
        assert previous_versions(self.VERSIONS, "3.1.2", floor="bad", count=1) == ["3.1.1"]

    def test_skips_unparseable_registry_entries(self):
        versions = ["3.0.0", "weird", "3.1.0", "3.1.2"]
        assert previous_versions(versions, "3.1.2", floor="3.0.0", count=99) \
            == ["3.1.0"]
