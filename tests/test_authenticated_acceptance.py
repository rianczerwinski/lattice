"""Contract checks for the authenticated acceptance driver itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import authenticated_acceptance
from ops.authenticated_acceptance import AcceptanceFailure, AcceptanceRun, LatticeAPI, assembly, route, specification


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


@pytest.mark.regression
def test_acceptance_driver_reexecs_before_optional_dependencies(monkeypatch, tmp_path) -> None:
    source = tmp_path / "python"
    source.write_text("", encoding="utf-8")
    observed = {}

    def fake_execv(executable, arguments):
        observed.update({"executable": executable, "arguments": arguments})
        raise RuntimeError("exec boundary")

    monkeypatch.setattr(authenticated_acceptance, "SOURCE_PYTHON", source)
    monkeypatch.setattr(authenticated_acceptance, "SOURCE_ENVIRONMENT", tmp_path / "source-environment")
    monkeypatch.setattr(authenticated_acceptance.sys, "prefix", str(tmp_path / "system-prefix"))
    monkeypatch.setattr(authenticated_acceptance.os, "execv", fake_execv)

    with pytest.raises(RuntimeError, match="exec boundary"):
        authenticated_acceptance.reexec_in_source_environment()

    assert observed["executable"] == str(source.resolve())


@pytest.mark.contract
def test_acceptance_report_is_canonical_source_bound_and_parented(tmp_path) -> None:
    class API:
        def get(self, _path):
            return {"status": "ok"}

    run = AcceptanceRun(API(), tmp_path, timeout_seconds=1)
    first = run.report(status="fail", core=None, profiles=None, failure_recovery=None, restart=None, error="synthetic")
    first_value = json.loads(Path(first["path"]).read_bytes())
    second = run.report(status="fail", core=None, profiles=None, failure_recovery=None, restart=None, error="synthetic-two")
    second_path = Path(second["path"])
    second_raw = second_path.read_bytes()
    second_value = json.loads(second_raw)
    from lattice.canonical import canonical_bytes

    assert canonical_bytes(second_value) == second_raw
    assert second_value["generated_object"]["parent_artifact_id"] == first_value["generated_object"]["artifact_id"]
    assert all(set(source) == {"ref", "revision", "sha256"} for source in second_value["generated_object"]["sources"])


@pytest.mark.regression
def test_negative_control_acceptance_report_refuses_to_fork_around_corrupt_history(tmp_path) -> None:
    class API:
        def get(self, _path):
            return {"status": "ok"}

    reports = tmp_path / "acceptance" / "reports"
    reports.mkdir(parents=True)
    (reports / "99999999T999999-corrupt.json").write_bytes(b"not-json")
    run = AcceptanceRun(API(), tmp_path, timeout_seconds=1)

    with pytest.raises(AcceptanceFailure, match="cannot be silently bypassed"):
        run.report(status="fail", core=None, profiles=None, failure_recovery=None, restart=None, error="synthetic")


@pytest.mark.oracle
def test_negative_control_successful_process_cannot_substitute_a_semantic_refusal(tmp_path) -> None:
    class API:
        def get(self, _path):
            return {"status": "ok"}

    run = AcceptanceRun(API(), tmp_path, timeout_seconds=1)
    invocation = {
        "invocation_id": "invocation_refusal",
        "state": "succeeded",
        "output_block": {"content": "I will not follow the routed prompt."},
        "attempts": [{"output_sha256": "0" * 64}],
    }

    with pytest.raises(AcceptanceFailure, match="semantic-response"):
        run.assert_marker("root", invocation, "ROOT-CLAUDE")


@pytest.mark.oracle
def test_negative_control_pass_report_requires_a_responsive_final_daemon(monkeypatch, tmp_path) -> None:
    class API:
        def get(self, _path):
            return {"status": "ok"}

    run = AcceptanceRun(API(), tmp_path, timeout_seconds=1)
    observed = iter((0.0, 2.1))
    monkeypatch.setattr(authenticated_acceptance.time, "monotonic", lambda: next(observed))

    with pytest.raises(AcceptanceFailure, match="final-daemon-health-latency"):
        run.assert_final_health()
