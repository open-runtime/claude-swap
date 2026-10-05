"""Localhost usage page: JSON from disk, no Anthropic calls."""

from __future__ import annotations

import json
import threading
from urllib.request import urlopen

from claude_swap.dashboard import _fable, _window, make_server
from claude_swap.tokens import TokenLedger


def test_windows_read_percents_only():
    usage = {
        "five_hour": {"pct": 17, "resets_at": "2026-10-05T21:40:00Z"},
        "seven_day": {"pct": 58},
        "scoped": [{"name": "Fable", "pct": 100, "resets_at": "2026-10-09T22:00:00Z"}],
    }
    assert _window(usage, "five_hour")["pct"] == 17
    assert _fable(usage)["pct"] == 100
    assert _window(None, "five_hour") is None


def _assistant(stamp: str, model: str, **usage) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": stamp,
        "message": {"model": model, "usage": usage},
    })


def test_token_ledger_records_fable_refusals_with_their_organization(tmp_path):
    session = tmp_path / "project" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("\n".join([
        json.dumps({"type": "attachment", "attachment": {"type": "credential_org", "organizationUuid": "org-a"}}),
        json.dumps({"type": "system", "subtype": "model_consent_fallback", "timestamp": "2026-10-05T19:46:59Z", "originalModel": "claude-fable-5-1", "fallbackModel": "claude-opus-5-5"}),
    ]) + "\n")
    now = 1791230400.0
    ledger = TokenLedger(tmp_path)
    ledger.refresh(now)
    assert len(ledger.refusals) == 1
    assert ledger.refusals[0].organization == "org-a"
    assert ledger.refused_since("org-a", now - 3600) is not None
    assert ledger.refused_since("org-b", now - 3600) is None


def test_token_ledger_attributes_calls_to_the_signed_in_organization(tmp_path):
    session = tmp_path / "project" / "session.jsonl"
    session.parent.mkdir(parents=True)
    lines = [
        json.dumps({"type": "attachment", "attachment": {"type": "credential_org", "organizationUuid": "org-a"}}),
        _assistant("2026-10-05T18:00:00Z", "claude-opus-5-5", input_tokens=10, output_tokens=20, cache_read_input_tokens=100, cache_creation_input_tokens=5),
        _assistant("2026-10-05T18:30:00Z", "claude-fable-5-1", input_tokens=1, output_tokens=2),
        json.dumps({"type": "attachment", "attachment": {"type": "credential_org", "organizationUuid": "org-b"}}),
        _assistant("2026-10-05T19:00:00Z", "claude-opus-5-5", input_tokens=3, output_tokens=4),
        _assistant("2026-10-05T19:01:00Z", "<synthetic>", input_tokens=99),
    ]
    session.write_text("\n".join(lines) + "\n")
    now = 1791230400.0  # 2026-10-05T20:00:00Z
    ledger = TokenLedger(tmp_path, horizon_s=7 * 24 * 3600.0)
    ledger.refresh(now)
    summary = ledger.summary(24 * 3600.0, now)
    assert summary["calls"] == 3
    assert summary["total"]["total"] == 10 + 20 + 100 + 5 + 1 + 2 + 3 + 4
    assert summary["byOrganization"]["org-a"]["calls"] == 2
    assert summary["byOrganization"]["org-b"]["total"] == 7
    assert set(summary["byModel"]) == {"claude-opus-5-5", "claude-fable-5-1"}
    assert [hour["at"] for hour in summary["hours"]] == [1791223200, 1791226800]

    # Appending reads only the tail and keeps the organization in force.
    with session.open("a") as handle:
        handle.write(_assistant("2026-10-05T19:30:00Z", "claude-opus-5-5", output_tokens=8) + "\n")
    ledger.refresh(now + 60)
    summary = ledger.summary(24 * 3600.0, now + 60)
    assert summary["calls"] == 4
    assert summary["byOrganization"]["org-b"]["output"] == 12


def test_page_and_state_are_served_on_loopback():
    state = {
        "strategy": "forecast",
        "model": "Fable",
        "threshold": 80,
        "active": "11",
        "decision": {"switchTo": None, "reason": "holding", "detail": "session 0%"},
        "accounts": [{
            "number": "11",
            "email": "enterprise@pieces.app",
            "organization": "Pieces",
            "active": True,
            "session": {"pct": 0},
            "weekly": {"pct": 0},
            "fable": {"pct": 0},
            "tokens": {"calls": 1, "total": 10, "output": 2},
            "readingAgeSeconds": 5,
            "lastError": None,
            "pollIntervalSeconds": 180,
            "consecutiveFailures": 0,
            "recent429": False,
        }],
        "samples": [{"at": 1, "session": 0, "fable": 0}],
        "tokens": {"calls": 1, "total": {"total": 10, "output": 2, "input": 1, "cacheRead": 0, "cacheCreation": 7, "calls": 1}, "byModel": {}, "hours": [], "files": 1, "ageSeconds": 1},
        "log": ["no switch: holding"],
    }
    seen: list[float] = []

    def reader(window_s: float) -> dict:
        seen.append(window_s)
        return state

    server = make_server(reader, 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page = urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
        body = json.loads(urlopen(f"http://127.0.0.1:{port}/api/state?window=3600").read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
    assert "Claude usage" in page
    assert body["active"] == "11"
    assert body["accounts"][0]["fable"]["pct"] == 0
    assert seen == [3600.0]
