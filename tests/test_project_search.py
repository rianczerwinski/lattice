"""Conversation and artifact search contracts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lattice.config import LatticeConfig
from lattice.contracts import ContextBlockCreate, DraftCreate
from lattice.security import ensure_cli_token
from lattice.server import create_app
from lattice.store import LatticeConflict

from conftest import completed_invocation


@pytest.mark.oracle
def test_search_finds_prompt_response_and_artifact_by_exact_substring(lattice_env) -> None:
    invocation = completed_invocation(lattice_env, "Find the needle in this prompt.", "The response hides NEEDLE-EXACT here.")
    lattice_env["store"].create_context_block(ContextBlockCreate(
        project_id=lattice_env["project"]["project_id"],
        semantic_type="note",
        content="unrelated",
        producing_invocation_id=invocation["invocation_id"],
        authority="test-oracle",
        evidence_level="planted",
    ))
    result = lattice_env["store"].search_project(lattice_env["project"]["project_id"], "NEEDLE-EXACT")

    assert result["query"] == "NEEDLE-EXACT"
    kinds = {item["kind"] for item in result["items"]}
    assert "response" in kinds
    assert any("NEEDLE-EXACT" in item["preview"] for item in result["items"])


@pytest.mark.oracle
def test_search_finds_the_original_user_prompt(lattice_env) -> None:
    draft = lattice_env["store"].create_draft(DraftCreate(
        project_id=lattice_env["project"]["project_id"],
        role_session_id=lattice_env["session"]["role_session_id"],
        prompt="PROMPT-CANARY-UNIQUE",
        route=lattice_env["route"],
    ))
    lattice_env["lattice"].seal(draft["draft_id"])
    result = lattice_env["store"].search_project(lattice_env["project"]["project_id"], "PROMPT-CANARY-UNIQUE")

    assert any(item["kind"] == "prompt" and "PROMPT-CANARY-UNIQUE" in item["preview"] for item in result["items"])


@pytest.mark.contract
def test_negative_control_search_rejects_newlines_and_empty_queries(lattice_env) -> None:
    with pytest.raises(LatticeConflict, match="malformed"):
        lattice_env["store"].search_project(lattice_env["project"]["project_id"], "a\nb")
    with pytest.raises(LatticeConflict, match="malformed"):
        lattice_env["store"].search_project(lattice_env["project"]["project_id"], "   ")


@pytest.mark.contract
def test_search_api_is_operator_bound(lattice_env) -> None:
    completed_invocation(lattice_env, "visible prompt", "visible response UNIQUE-HIT")
    app = create_app(lattice_env["config"])
    token = ensure_cli_token(lattice_env["config"].operator_token)
    with TestClient(app) as client:
        denied = client.get(f"/api/v1/projects/{lattice_env['project']['project_id']}/search", params={"q": "UNIQUE-HIT"})
        session = client.get("/api/session", headers={"authorization": f"Bearer {token}"})
        allowed = client.get(
            f"/api/v1/projects/{lattice_env['project']['project_id']}/search",
            params={"q": "UNIQUE-HIT"},
            headers={"origin": "http://testserver"},
            cookies=session.cookies,
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["items"]
