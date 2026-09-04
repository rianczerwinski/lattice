"""Contract checks for the thin authenticated Lattice client."""

from __future__ import annotations

from lattice.client import LatticeClient


def test_contract_cancel_attempt_uses_the_attempt_resource(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"attempt_id": "attempt/a", "state": "cancelling"}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")

    result = client.cancel_attempt("attempt/a")

    assert result == {"attempt_id": "attempt/a", "state": "cancelling"}
    assert calls == [("POST", "/api/v1/attempts/attempt%2Fa/cancel", {})]
