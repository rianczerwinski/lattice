"""Contract checks for the authenticated acceptance driver itself."""

from __future__ import annotations

import pytest

from ops.authenticated_acceptance import AcceptanceFailure, LatticeAPI, assembly, route, specification


@pytest.mark.contract
def test_acceptance_specs_keep_direct_actor_and_route_authority_explicit() -> None:
    context = assembly("project_test", "Synthetic prompt", parents=["invocation_parent"])
    codex = route("openai", execution_profile="workspace-agent", workspace="/tmp/workspace", tools=["shell"], sandbox="read-only")
    value = specification("codex-workspace", context, codex, concerns=["slate:synthetic"])

    assert value["assembly"]["actor_binding"] == {"kind": "direct"}
    assert value["assembly"]["parent_invocation_ids"] == ["invocation_parent"]
    assert value["route"]["tools"] == ["shell"]
    assert value["route"]["execution_profile"] == "workspace-agent"
    assert value["concern_refs"] == ["slate:synthetic"]


@pytest.mark.regression
def test_negative_control_acceptance_rejection_must_match_the_expected_boundary(monkeypatch) -> None:
    api = LatticeAPI("http://127.0.0.1:9010", "synthetic-token")
    monkeypatch.setattr(api, "post", lambda _path, _payload: (_ for _ in ()).throw(AcceptanceFailure("HTTP 409: different boundary")))

    with pytest.raises(AcceptanceFailure, match="did not contain"):
        api.expect_rejected("/preview", {}, contains="authority boundaries")
