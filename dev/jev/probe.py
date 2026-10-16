"""Exercise shadow plumbing with synthetic tasks and a fixed authoritative pick.

Run from the repository root: python -m dev.jev.probe --receipts PATH --count 50.
Uses the existing balance and the same persistent fork ledger as the server.
This proves noninterference; it is not a production routing-quality evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from omnigent.server.jev_receipts import JevReceiptStore
from omnigent.server.routing_backend import (
    RoutingBackends,
    drain_shadow_observers,
    route_with_fallback,
)
from omnigent.server.smart_routing import JevShadowRouting, RoutingResult, infer_models

_TASKS = (
    "Correct a spelling error in a README.",
    "Add pagination to an existing REST endpoint and verify boundary cases.",
    "Design a migration from a single database to regional replicas with conflict resolution.",
    "Explain how a Python list comprehension works.",
    "Investigate an intermittent race in an asynchronous job scheduler.",
)


async def probe(path: Path, count: int) -> None:
    """Use live Jev with a synthetic, deterministic existing-router fixture."""
    models = infer_models("codex")
    if not models:
        raise RuntimeError("No candidate models")
    judge = RoutingResult(models[0], "Synthetic probe baseline", "codex")

    class Baseline:
        async def route(
            self, message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult:
            if judge.model not in available_models.get("codex", []) or not message:
                raise ValueError("Invalid probe input")
            return judge

    first_line = len(path.read_text().splitlines()) if path.exists() else 0
    backends = RoutingBackends(local=Baseline(), shadow=JevShadowRouting("shadow", str(path)))
    for i in range(count):
        result = await route_with_fallback(
            backends,
            _TASKS[i % len(_TASKS)],
            {"codex": models},
            gateway_backed=False,
        )
        await drain_shadow_observers()
        if result is None or result.result is not judge:
            raise RuntimeError("Authoritative pick changed")
        print(f"probe {i + 1}/{count}: authoritative pick preserved", flush=True)
    rows = [json.loads(line) for line in path.read_text().splitlines()[first_line:]]
    pairs = [row for row in rows if row.get("kind") == "paired"]
    JevReceiptStore(path).append(
        {
            "kind": "probe_manifest",
            "cohort": "synthetic_fixed_baseline",
            "paired_records": len(pairs),
            "changed_model_choices": 0,
            "first_pair_id": pairs[0]["pair_id"] if pairs else None,
            "last_pair_id": pairs[-1]["pair_id"] if pairs else None,
        }
    )
    print(
        json.dumps(
            {
                "paired": len(pairs),
                "followed": sum(row["followed"] for row in pairs),
                "shadow_picks": sum(row["status"] == "picked" for row in pairs),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1, choices=range(1, 59))
    args = parser.parse_args()
    asyncio.run(probe(args.receipts, args.count))
