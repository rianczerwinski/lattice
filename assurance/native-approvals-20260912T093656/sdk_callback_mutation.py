"""Fault injection in an in-memory SDK callback; no provider or production DB."""

import hashlib
from pathlib import Path
import sys
import types

import pytest

from lattice.execution import native_approval
from lattice.execution.journal import write_new_json


mode, destination = sys.argv[1:]
output = Path(destination).resolve()
if output.exists() or output.with_suffix(".input.json").exists():
    raise FileExistsError("mutation evidence cannot be reused")
root = Path(__file__).resolve().parents[2]
source_path = Path(native_approval.__file__)
test_path = root / "tests/test_native_approval_sdk.py"
source = source_path.read_text()
if mode == "mutable-input":
    old = 'updated_input=decision["binding"]["action"]["tool_input"]'
    new = "updated_input=tool_input"
    selector = "test_callback_waits_for_operator_then_returns_exact_persisted_action[allow]"
    method = "__call__"
    rejected_text = "response.updated_input"
elif mode == "lost-failure-signal":
    old = "        self.cancel.set()"
    new = "        pass  # planted missing process-cancellation signal"
    selector = "test_persistence_failure_cannot_be_hidden_by_sdk_continuation[request]"
    method = "_fail"
    rejected_text = "native.cancel.is_set"
else:
    raise ValueError(mode)
assert source.count(old) == 1
mutant = types.ModuleType("lattice.execution._approval_mutant")
mutant.__package__ = "lattice.execution"
exec(compile(source.replace(old, new), str(source_path), "exec"), mutant.__dict__)
bindings = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [source_path, test_path, root / "lattice/native_approvals.py", root / "uv.lock", Path(__file__)]}


class Observation:
    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        self.reports.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome, "detail": str(report.longrepr) if report.failed else None})


write_new_json(output.with_suffix(".input.json"), {"retention": "keep", "mode": mode, "source_bindings": bindings, "selector": selector, "scope": "in-memory callback fault and planted local ledger; no SDK/native provider or production database", "disposable": "pytest-owned planted database; decisive reports retained"})
observer = Observation()
original = getattr(native_approval.NativeApprovalCallback, method)
setattr(native_approval.NativeApprovalCallback, method, getattr(mutant.NativeApprovalCallback, method))
try:
    code = pytest.main([str(test_path) + "::" + selector, "-q"], plugins=[observer])
finally:
    setattr(native_approval.NativeApprovalCallback, method, original)
failures = [item for item in observer.reports if item["outcome"] == "failed"]
rejected = code == 1 and len(failures) == 1 and failures[0]["when"] == "call" and rejected_text in failures[0]["detail"]
write_new_json(output, {"source_bindings": bindings, "pytest_exit_code": int(code), "rejected_at_intended_assertion": rejected, "reports": observer.reports})
if not rejected:
    raise SystemExit("mutation was not rejected at the intended assertion")
