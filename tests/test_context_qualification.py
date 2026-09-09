"""Planted-answer and false-receipt controls for the bounded live canary."""

import copy
import json

import pytest

from ops.qualify_context import receipt_check, semantic_check, write_once


@pytest.mark.oracle
def test_answer_oracle_rejects_stale_edit_false_consensus_and_fabricated_quote():
    joined = {"id": "edited_join", "expected": {"alpha": 29, "beta": 23, "second_harness": "claude"}}
    assert semantic_check(joined, joined["expected"])
    assert not semantic_check(joined, {**joined["expected"], "alpha": 17})
    assert not semantic_check(joined, {**joined["expected"], "second_harness": "codex"})
    conflict = {"id": "conflicting_references", "expected": {"left": 7, "right": 9, "conflict": True, "marker": "Ä-STATE"}}
    assert semantic_check(conflict, conflict["expected"])
    assert not semantic_check(conflict, {**conflict["expected"], "conflict": False})
    case = {"id": "eligibility", "evidence": [{"evidence_id": "source", "text": "A guide to reference works in philosophy."}]}
    correct = {"family_id": "qef_fixture", "outcome": "retain", **{key: {"evidence_id": "source", "quote": "reference works in philosophy"} for key in ("identity", "structural", "mission")}}
    assert semantic_check(case, correct)
    for field, value in [("evidence_id", "fabricated"), ("quote", "not in source"), ("quote", "")]:
        wrong = copy.deepcopy(correct)
        wrong["mission"][field] = value
        assert not semantic_check(case, wrong)


@pytest.mark.oracle
def test_receipt_oracle_rejects_model_switch_refusal_and_changed_prompt(tmp_path):
    invocation = {"compiled_prompt": "exact", "compiled_prompt_sha256": "fixture"}
    request = {"model": "claude-opus-5", "effort": "high", "system_prompt": None, "prompt_sha256": "fixture"}
    event = {"type": "result", "is_error": False, "modelUsage": {"claude-opus-5": {}}}
    (tmp_path / "request.json").write_text(json.dumps(request))
    (tmp_path / "stdout.log").write_text(json.dumps(event) + "\n")
    (tmp_path / "prompt.txt").write_bytes(b"exact")
    assert receipt_check(invocation, tmp_path)
    for mutation in [{**event, "is_error": True}, {**event, "modelUsage": {"different-model": {}}}]:
        (tmp_path / "stdout.log").write_text(json.dumps(mutation) + "\n")
        assert not receipt_check(invocation, tmp_path)
    (tmp_path / "stdout.log").write_text(json.dumps(event) + "\n")
    (tmp_path / "prompt.txt").write_bytes(b"changed")
    assert not receipt_check(invocation, tmp_path)


@pytest.mark.contract
def test_started_and_result_records_cannot_be_overwritten(tmp_path):
    target = tmp_path / "case.started.json"
    write_once(target, {"started": True})
    with pytest.raises(FileExistsError):
        write_once(target, {"started": False})
    assert json.loads(target.read_bytes()) == {"started": True}
