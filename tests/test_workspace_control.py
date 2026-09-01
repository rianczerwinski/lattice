from __future__ import annotations

import hashlib
import shlex
import subprocess

import pytest
from fastapi.testclient import TestClient

from lattice.config import LatticeConfig
from lattice.security import ensure_cli_token
from lattice.server import create_app
from lattice.workspace_control import WorkspaceControl, WorkspaceControlError


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.mark.oracle
def test_file_view_preserves_exact_bytes_and_line_address(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    content = "alpha\nbeta\ngamma\ndelta\n"
    (repo / "sample.py").write_text(content)
    control = WorkspaceControl(repo, allowed_roots=(tmp_path,))

    view = control.read_file("sample.py", start_line=2, line_count=2)

    assert view.content == "beta\ngamma\n"
    assert (view.start_line, view.end_line, view.total_lines) == (2, 3, 4)
    assert view.truncated_before is True and view.truncated_after is True
    assert view.content_sha256 == hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.contract
def test_workspace_and_symlink_escape_are_refused(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("not in the workspace")
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    control = WorkspaceControl(repo, allowed_roots=(tmp_path,))

    with pytest.raises(WorkspaceControlError, match="escapes"):
        control.read_file("escape/secret.txt")
    with pytest.raises(WorkspaceControlError, match="outside"):
        WorkspaceControl(outside, allowed_roots=(repo,))


@pytest.mark.property
def test_path_text_and_symbol_search_are_stable_and_addressable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "zeta.py").write_text("def sought_symbol():\n    return 'needle'\n")
    (repo / "alpha.py").write_text("needle = 1\n")
    control = WorkspaceControl(repo, allowed_roots=(tmp_path,))

    assert [hit.path for hit in control.search(".py", mode="path")] == ["alpha.py", "zeta.py"]
    assert [(hit.path, hit.line) for hit in control.search("needle")] == [("alpha.py", 1), ("zeta.py", 2)]
    assert [(hit.path, hit.line) for hit in control.search("sought_symbol", mode="symbol")] == [("zeta.py", 1)]


@pytest.mark.contract
def test_git_review_distinguishes_unstaged_staged_and_revision(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Lattice Test")
    (repo / "note.txt").write_text("one\n")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "note.txt").write_text("one\ntwo\n")
    control = WorkspaceControl(repo, allowed_roots=(tmp_path,))

    assert control.git_status()["clean"] is False
    assert "+two" in control.git_diff(view="unstaged")["diff"]
    _git(repo, "add", "note.txt")
    assert control.git_diff(view="unstaged")["empty"] is True
    assert "+two" in control.git_diff(view="staged")["diff"]
    assert "+two" in control.git_diff(view="revision", revision=base)["diff"]


@pytest.mark.contract
def test_git_review_does_not_execute_repository_configured_helpers(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Lattice Test")
    (repo / ".gitattributes").write_text("note.txt diff=hostile\n")
    (repo / "note.txt").write_text("one\n")
    _git(repo, "add", ".gitattributes", "note.txt")
    _git(repo, "commit", "-q", "-m", "initial")

    marker = tmp_path / "repository-helper-ran"
    helper = tmp_path / "repository-helper.sh"
    helper.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nprintf '0\\n'\n")
    helper.chmod(0o700)
    _git(repo, "config", "core.fsmonitor", str(helper))
    _git(repo, "config", "diff.external", str(helper))
    _git(repo, "config", "diff.hostile.textconv", str(helper))
    (repo / "note.txt").write_text("one\ntwo\n")
    control = WorkspaceControl(repo, allowed_roots=(tmp_path,))

    assert control.git_status()["clean"] is False
    assert "+two" in control.git_diff(view="unstaged")["diff"]
    assert not marker.exists()


@pytest.mark.contract
def test_revision_cannot_be_reinterpreted_as_an_option(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    control = WorkspaceControl(repo, allowed_roots=(tmp_path,))

    with pytest.raises(WorkspaceControlError, match="explicit revision"):
        control.git_diff(view="revision", revision="--output=/tmp/escape")


@pytest.mark.contract
def test_workspace_api_is_operator_bound_and_returns_addressable_views(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "source.py").write_text("def example():\n    return 'visible'\n")
    config = LatticeConfig(data_root=tmp_path / "data", axisvi_root=tmp_path)
    token = ensure_cli_token(config.operator_token)
    app = create_app(config)

    with TestClient(app) as client:
        denied = client.post("/api/v1/workspaces/file", json={"workspace": str(repo), "path": "source.py"})
        session = client.get("/api/session", headers={"authorization": f"Bearer {token}"})
        headers = {"x-lattice-csrf": session.json()["csrf"], "origin": "http://testserver"}
        file_view = client.post("/api/v1/workspaces/file", headers=headers, json={"workspace": str(repo), "path": "source.py"})
        symbols = client.post("/api/v1/workspaces/search", headers=headers, json={"workspace": str(repo), "mode": "symbol", "query": "example"})
        escaped = client.post("/api/v1/workspaces/file", headers=headers, json={"workspace": str(repo), "path": "../outside"})

    assert denied.status_code == 403
    assert file_view.status_code == 200
    assert file_view.json()["content"] == "def example():\n    return 'visible'\n"
    assert symbols.json() == [{"path": "source.py", "line": 1, "column": 1, "preview": "def example():"}]
    assert escaped.status_code == 422
