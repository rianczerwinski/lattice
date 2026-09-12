"""Falsify exact binding or one-use release in memory, with planted test data."""

import hashlib
from pathlib import Path
import sys
import types

import pytest

from lattice import native_approvals
from lattice.execution.journal import write_new_json


root = Path(__file__).resolve().parent
mode = sys.argv[1]
output = Path(sys.argv[2]).resolve()
if output.exists():
    raise FileExistsError(output)
source_path = Path(native_approvals.__file__)
test_path = root.parents[1] / "tests/test_native_approvals.py"
source = source_path.read_text()
if mode == "ignore-binding":
    selector = "test_action_or_sibling_identity_cannot_reuse_a_grant"
    native_approvals._bound = lambda *_args: None
    mutation = "skip the exact request/action binding check at decision delivery"
elif mode == "repeat-release":
    target = '        if item["state"] != "decided":'
    assert source.count(target) == 1
    copy = types.ModuleType("lattice._native_approval_mutant")
    copy.__package__ = "lattice"
    exec(compile(source.replace(target, '        if item["state"] not in {"decided", "released-to-adapter"}:'), str(source_path), "exec"), copy.__dict__)
    native_approvals.take_decision = copy.take_decision
    selector = "test_exact_decision_is_persistent_and_released_once[allow]"
    mutation = "allow an already released decision to be consumed a second time"
else:
    raise ValueError(mode)


class Observation:
    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        self.reports.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome, "detail": str(report.longrepr) if report.failed else None})


observer = Observation()
bindings = {str(path.relative_to(root.parents[1])): hashlib.sha256(path.read_bytes()).hexdigest() for path in [source_path, test_path, root.parents[1] / "lattice/store.py", root.parents[1] / "lattice/contracts.py", Path(__file__)]}
write_new_json(output.with_suffix(".input.json"), {"mode": mode, "mutation": mutation, "source_bindings": bindings, "selector": selector, "retention": "keep", "scope": "in-memory local ledger fault injection; no provider process or production database", "disposable": "pytest-owned planted temporary data; decisive reports retained"})
exit_code = pytest.main([str(test_path) + "::" + selector, "-q"], plugins=[observer])
failures = [report for report in observer.reports if report["outcome"] == "failed"]
rejected = exit_code == 1 and len(failures) == 1 and failures[0]["when"] == "call" and "DID NOT RAISE" in failures[0]["detail"] and "LatticeConflict" in failures[0]["detail"]
write_new_json(output, {"mode": mode, "source_bindings": bindings, "pytest_exit_code": int(exit_code), "rejected_at_intended_assertion": rejected, "reports": observer.reports})
if not rejected:
    raise SystemExit("mutation was not rejected at the intended assertion")
