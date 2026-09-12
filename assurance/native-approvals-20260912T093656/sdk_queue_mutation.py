"""Recreate unbounded input-entry admission in memory; no provider invocation."""

import hashlib
from pathlib import Path
import queue
import sys

import pytest

from lattice.execution import sdk_transport
from lattice.execution.journal import write_new_json


output = Path(sys.argv[1]).resolve()
if output.exists() or output.with_suffix(".input.json").exists():
    raise FileExistsError("mutation evidence cannot be reused")
root = Path(__file__).resolve().parents[2]
test = root / "tests/test_sdk_transport.py"
selector = "test_stalled_input_is_bounded_and_shutdown_does_not_wait_for_space[space]"
bindings = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [Path(sdk_transport.__file__), test, root / "uv.lock", Path(__file__)]}
original_init = sdk_transport.GuardedSDKTransport.__init__


def unbounded(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self._inputs = queue.Queue()


class Observation:
    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        self.reports.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome, "detail": str(report.longrepr) if report.failed else None})


write_new_json(output.with_suffix(".input.json"), {"retention": "keep", "mutation": "remove the native input-entry bound", "source_bindings": bindings, "selector": selector, "scope": "in-memory transport mutation with a planted consumer; no native CLI or tool execution", "disposable": "pytest-owned planted temporary journal; decisive reports retained"})
observer = Observation()
sdk_transport.GuardedSDKTransport.__init__ = unbounded
try:
    code = pytest.main([str(test) + "::" + selector, "-q"], plugins=[observer])
finally:
    sdk_transport.GuardedSDKTransport.__init__ = original_init
failures = [item for item in observer.reports if item["outcome"] == "failed"]
rejected = code == 1 and len(failures) == 1 and failures[0]["when"] == "call" and "payload entries need a finite bound plus an EOF slot" in failures[0]["detail"]
write_new_json(output, {"source_bindings": bindings, "pytest_exit_code": int(code), "rejected_at_intended_assertion": rejected, "reports": observer.reports})
if not rejected:
    raise SystemExit("mutation was not rejected at the intended assertion")
