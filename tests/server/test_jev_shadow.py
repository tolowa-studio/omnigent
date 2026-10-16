"""Shadow routing must preserve authoritative decisions and bound spend."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.server.jev_receipts import (
    DAILY_CAP_NANOUSD,
    PAIR_RESERVATION_NANOUSD,
    JevReceiptStore,
    digest,
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
