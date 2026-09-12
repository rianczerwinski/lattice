"""Exercise permanent pipe oracles against named in-memory transport defects."""

import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import traceback
import types

import pytest

from lattice.execution import process
from lattice.execution.journal import write_new_json


root = Path(__file__).resolve().parent
source_path = Path(process.__file__)
test_path = root.parents[1] / "tests/test_process_duplex.py"
source = source_path.read_text()
mode = sys.argv[2] if len(sys.argv) > 2 else "premature-eof"
write = "                    proc.stdin.write(message)\n                    proc.stdin.flush()"
assert source.count(write) == 1
if mode == "premature-eof":
    mutated = source.replace(write, write + "\n                    break  # planted premature EOF")
    selector = "test_reply_after_observed_request_preserves_exact_bytes_and_eof"
    assertion = 'assert result.stdout == "request\\nréponse\\n"'
    description = "close stdin after the first queued message"
elif mode == "late-input-bound":
    budget = '                    input_bytes += len(message)\n                    if input_bytes > max_input_bytes:\n                        raise ProcessInputLimit(f"process input exceeds its {max_input_bytes}-byte input limit")'
    assert source.count(budget + "\n" + write) == 1
    mutated = source.replace(budget + "\n" + write, write + "\n" + budget)
    selector = "test_streaming_input_budget_rejects_frame_before_delivery"
    assertion = 'assert stdin_writes[0] == [b"first\\n"]'
    description = "check the cumulative input bound only after delivering the prohibited frame"
else:
    raise ValueError(f"unknown mutation: {mode}")
copy = types.ModuleType("lattice.execution._duplex_mutation")
copy.__package__ = "lattice.execution"
exec(compile(mutated, str(source_path), "exec"), copy.__dict__)
tests = runpy.run_path(str(test_path))
check = tests[selector]
check.__globals__["run_bounded"] = copy.run_bounded
check.__globals__["ProcessInputLimit"] = copy.ProcessInputLimit
with tempfile.TemporaryDirectory(prefix="lattice-duplex-mutation-") as directory, pytest.MonkeyPatch.context() as patch:
    args = [Path(directory)]
    if mode == "late-input-bound":
        args.append(tests["stdin_writes"].__wrapped__(patch))
    try:
        check(*args)
    except AssertionError:
        failure = traceback.format_exc()
        if assertion not in failure:
            raise
        observation = {"mutation": description, "result": "rejected-at-intended-assertion", "assertion": assertion, "traceback": failure}
    else:
        raise AssertionError(f"{mode} mutation survived its permanent oracle")
observation.update(source_sha256=hashlib.sha256(source.encode()).hexdigest(), mutant_sha256=hashlib.sha256(mutated.encode()).hexdigest(), test_sha256=hashlib.sha256(test_path.read_bytes()).hexdigest(), selector=check.__name__, isolated_copy="in-memory; only planted pipe fixtures used temporary storage")
write_new_json(Path(sys.argv[1]) if len(sys.argv) > 1 else root / "duplex-mutation.log", observation)
print(json.dumps(observation))
