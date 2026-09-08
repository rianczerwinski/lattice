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


def test_contract_route_decision_is_a_separate_exact_resource(monkeypatch) -> None:
    calls = []

    def request(_self, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"state": "active"}

    monkeypatch.setattr(LatticeClient, "_request", request)
    client = LatticeClient("http://127.0.0.1:9010", "token", "127.0.0.1:9010")
    client.route_amendments("session/a")
    client.decide_route_amendment("session/a", "amendment/b", decision="approve", note="Explicit operator approval")
    assert calls == [
        ("GET", "/api/v1/role-sessions/session%2Fa/route-amendments", None),
        ("POST", "/api/v1/role-sessions/session%2Fa/route-amendments/amendment%2Fb/decision", {"decision": "approve", "note": "Explicit operator approval"}),
    ]
