"""Shadow routing must preserve authoritative decisions and bound spend."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.server.jev_receipts import (
    DAILY_CAP_NANOUSD,
    PAIR_RESERVATION_NANOUSD,
    JevReceiptStore,
    digest,
    utc_budget_open,
)
from omnigent.server.routing_backend import (
    RoutingBackends,
    drain_shadow_observers,
    route_with_fallback,
)
from omnigent.server.smart_routing import JevShadowRouting, RoutingResult


@pytest.mark.parametrize("gateway", [True, False])
async def test_paired_shadow_preserves_judge(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    requests = []

    async def post(self, url, **kwargs):
        payload = kwargs["json"]
        requests.append(payload)
        assert payload["model"] == "jev-1.13.0"
        choices = payload["questions"]["pick"]["criteria"]
        choice = "MODERATE" if "MODERATE" in choices else "1"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "jev-1.13.0",
                "answers": {"pick": {"type": "choice", "choice": choice}},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = tmp_path / "receipts.jsonl"
    judge = RoutingResult("small", "private rationale", "codex")
    client = AsyncMock()
    client.route.return_value = judge
    backends = RoutingBackends(
        external=client, local=client, shadow=JevShadowRouting("shadow", str(path))
    )
    for _ in range(50):
        result = await route_with_fallback(
            backends, "private task", {"codex": ["small", "large"]}, gateway_backed=gateway
        )
        await drain_shadow_observers()
        assert result.result is judge
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    paired = [row for row in rows if row["kind"] == "paired"]
    assert len(paired) == 50
    assert all(row["followed"] and row["agreement"] is False for row in paired)
    assert all(row["effect"] == "none" for row in paired)
    assert len(requests) == 100
    assert "private" not in path.read_text()
    assert "test-secret" not in path.read_text()
    previous = None
    for row in rows:
        claimed = row.pop("receipt_hash")
        assert row["previous_hash"] == previous
        assert digest(row) == claimed
        previous = claimed


@pytest.mark.parametrize("failure", ["timeout", "invalid", "escape", "version"])
async def test_failures_never_change_pick(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    calls = []

    async def post(self, url, **kwargs):
        calls.append(url)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret server response")
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "wrong" if failure == "version" else "jev-1.13.0",
                "answers": {
                    "pick": {
                        "type": "choice",
                        "choice": "ESCAPE" if failure == "escape" else "invalid",
                    }
                },
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = tmp_path / "receipts.jsonl"
    judge = RoutingResult("small", "reason", "codex")
    client = AsyncMock()
    client.route.return_value = judge
    result = await route_with_fallback(
        RoutingBackends(local=client, shadow=JevShadowRouting("shadow", str(path))),
        "task",
        {"codex": ["small"]},
        gateway_backed=False,
    )
    await drain_shadow_observers()
    assert result.result is judge
    paired = json.loads(path.read_text().splitlines()[-1])
    assert paired["jev_pick_hash"] is None
    assert paired["followed"] is True
    assert len(calls) == 1
    assert "secret" not in path.read_text()


def test_budget_atomic_and_persistent(tmp_path):
    path = tmp_path / "receipts.jsonl"

    def reserve(_):
        return JevReceiptStore(path).append(
            {"reserved_nanousd": PAIR_RESERVATION_NANOUSD}, reserve=True
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = sum(pool.map(reserve, range(80)))
    assert accepted == DAILY_CAP_NANOUSD // PAIR_RESERVATION_NANOUSD
    assert not reserve(None)
    assert accepted * PAIR_RESERVATION_NANOUSD <= DAILY_CAP_NANOUSD
    with path.open("a") as stream:
        stream.write('{"tampered":true}\n')
    with pytest.raises(KeyError):
        reserve(None)


@pytest.mark.parametrize(
    "mode,key,path_enabled",
    [("off", True, True), ("shadow", False, True), ("shadow", True, False)],
)
async def test_disabled_makes_no_requests(tmp_path, monkeypatch, mode, key, path_enabled):
    monkeypatch.setenv("TYPESAFE_API_KEY", "key" if key else "")
    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await JevShadowRouting(mode, str(tmp_path / "rows") if path_enabled else "").observe(
        "task", {"codex": ["small"]}, None, "oss-llm"
    )
    post.assert_not_called()
    assert not (tmp_path / "rows").exists()


def test_no_active_mode():
    with pytest.raises(ValueError, match="off or shadow"):
        JevShadowRouting("active")


async def test_observer_failure_preserves_result():
    judge = RoutingResult("small", "reason", "codex")
    client = AsyncMock()
    client.route.return_value = judge
    observer = AsyncMock()
    observer.mode = "shadow"
    observer.observe.side_effect = OSError("unavailable storage")
    result = await route_with_fallback(
        RoutingBackends(local=client, shadow=observer),
        "task",
        {"codex": ["small"]},
        gateway_backed=False,
    )
    await drain_shadow_observers()
    assert result.result is judge


def test_runtime_caps_observer_reaches_both_backends(tmp_path):
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.routing_backend import backends_from_caps

    shadow = JevShadowRouting("shadow", str(tmp_path / "receipts"))
    client = AsyncMock()
    caps = RuntimeCaps(routing_backends=RoutingBackends(local=client), jev_shadow=shadow)
    assert backends_from_caps(caps).shadow is shadow
    assert backends_from_caps(caps).local is client


def test_backends_from_caps_keeps_jev_shadow_without_routing_client(tmp_path):
    from omnigent.server.routing_backend import backends_from_caps

    shadow = JevShadowRouting("shadow", str(tmp_path / "receipts"))
    caps = SimpleNamespace(routing_backends=None, routing_client=None, jev_shadow=shadow)
    backends = backends_from_caps(caps)
    assert backends.shadow is shadow
    assert backends.local is None and backends.external is None


def test_release_reservation_net_zero_spend(tmp_path):
    path = tmp_path / "receipts.jsonl"
    store = JevReceiptStore(path)
    assert store.append(
        {
            "kind": "reservation",
            "pair_id": "pair-1",
            "reserved_nanousd": PAIR_RESERVATION_NANOUSD,
        },
        reserve=True,
    )
    store.release_reservation("pair-1")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    day = rows[0]["day"]
    spent = sum(row.get("reserved_nanousd", 0) for row in rows if row["day"] == day)
    assert spent == 0
    assert any(row.get("kind") == "reservation_release" for row in rows)


def test_receipt_store_does_not_mutate_persisted_rows(tmp_path):
    path = tmp_path / "receipts.jsonl"
    store = JevReceiptStore(path)
    store.append({"kind": "probe", "note": "first"})
    before = path.read_text()
    store.append({"kind": "probe", "note": "second"})
    assert before in path.read_text()
    first_line = json.loads(before.splitlines()[0])
    assert "receipt_hash" in first_line


@pytest.mark.parametrize("second", [49, 50])
async def test_budget_boundary_refunds_reservation(tmp_path, monkeypatch, second):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    frozen = datetime(2026, 1, 1, 23, 59, second, tzinfo=UTC)
    monkeypatch.setattr("omnigent.server.jev_receipts.utc_now", lambda: frozen)
    assert utc_budget_open(frozen) == (second < 50)

    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = tmp_path / "receipts.jsonl"
    await JevShadowRouting("shadow", str(path)).observe(
        "task", {"codex": ["small"]}, None, "oss-llm"
    )
    if second >= 50:
        assert post.await_count == 0
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert not any(row.get("kind") == "reservation" for row in rows)
    else:
        post.assert_awaited()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert any(row.get("kind") == "reservation" for row in rows)


async def test_typesafe_key_not_logged_in_httpx_debug(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TYPESAFE_API_KEY", "super-secret-key")
    caplog.set_level(logging.DEBUG, logger="httpx")

    async def post(self, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "jev-1.13.0",
                "answers": {"pick": {"type": "choice", "choice": "ESCAPE"}},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = tmp_path / "receipts.jsonl"
    await JevShadowRouting("shadow", str(path)).observe(
        "task", {"codex": ["small"]}, None, "oss-llm"
    )
    assert "super-secret-key" not in caplog.text


async def test_day_rollover_refunds_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    calls = 0

    def stepping_utc_now():
        nonlocal calls
        calls += 1
        if calls >= 3:
            return datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
        return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr("omnigent.server.jev_receipts.utc_now", stepping_utc_now)

    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    releases: list[tuple[str, str | None]] = []
    original_release = JevReceiptStore.release_reservation

    def track_release(self, pair_id: str, *, ledger_day: str | None = None) -> None:
        releases.append((pair_id, ledger_day))
        original_release(self, pair_id, ledger_day=ledger_day)

    monkeypatch.setattr(JevReceiptStore, "release_reservation", track_release)
    path = tmp_path / "receipts.jsonl"
    await JevShadowRouting("shadow", str(path)).observe(
        "task", {"codex": ["small"]}, None, "oss-llm"
    )
    post.assert_not_awaited()
    assert releases == [pytest.approx(())] or releases  # noqa: B015 - ensure release ran
    assert releases and releases[0][1] == "2026-01-01"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    reserved = sum(row.get("reserved_nanousd", 0) for row in rows if row["day"] == "2026-01-01")
    assert reserved == 0


async def test_shared_typesafe_client_across_pair(tmp_path, monkeypatch):
    import omnigent.server.smart_routing as smart_routing

    monkeypatch.setattr(smart_routing, "_jev_typesafe_http", None)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    created: list[httpx.AsyncClient] = []
    real_init = httpx.AsyncClient.__init__

    def track_init(self, *args, **kwargs):
        created.append(self)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", track_init)

    async def post(self, url, **kwargs):
        choices = kwargs["json"]["questions"]["pick"]["criteria"]
        choice = "MODERATE" if "MODERATE" in choices else "0"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "jev-1.13.0",
                "answers": {"pick": {"type": "choice", "choice": choice}},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = tmp_path / "receipts.jsonl"
    await JevShadowRouting("shadow", str(path)).observe(
        "task", {"codex": ["small", "large"]}, None, "oss-llm"
    )
    assert len(created) == 1


async def test_slow_observer_never_delays_routing():
    judge = RoutingResult("small", "reason", "codex")
    client = AsyncMock()
    client.route.return_value = judge
    release = asyncio.Event()
    observer = AsyncMock()
    observer.mode = "shadow"

    async def slow(*args):
        await release.wait()

    observer.observe.side_effect = slow
    try:
        result = await asyncio.wait_for(
            route_with_fallback(
                RoutingBackends(local=client, shadow=observer),
                "task",
                {"codex": ["small"]},
                gateway_backed=False,
            ),
            timeout=0.1,
        )
        assert result.result is judge
    finally:
        release.set()
        await drain_shadow_observers()
