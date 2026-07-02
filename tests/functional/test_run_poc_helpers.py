"""Functional: run_poc helper functions (workspace prep + robust rmtree)."""
import os
import stat
import subprocess

import pytest

import run_poc

pytestmark = pytest.mark.functional


def test_rmtree_removes_readonly_files(tmp_path):
    # git pack objects are read-only on Windows; _on_rm_error must clear the bit.
    d = tmp_path / "tree"
    d.mkdir()
    f = d / "readonly.bin"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, stat.S_IREAD)
    run_poc._rmtree(d)
    assert not d.exists()


def test_prepare_workspace_initializes_git(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    ws = tmp_path / "ws"

    run_poc.prepare_workspace(source, ws, "main")

    assert (ws / ".git").exists()
    assert (ws / "requirements.txt").read_text() == "flask==3.0.0\n"
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=ws,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "main"
    # a clean initial commit exists
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws,
                         capture_output=True, text=True, check=True).stdout
    assert "initial import" in log


def test_prepare_workspace_overwrites_existing(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("1", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "stale.txt").write_text("old", encoding="utf-8")  # must be wiped

    run_poc.prepare_workspace(source, ws, "main")

    assert not (ws / "stale.txt").exists()
    assert (ws / "a.txt").exists()
