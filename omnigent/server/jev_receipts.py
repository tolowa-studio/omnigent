"""Locked, append-only shadow receipts and conservative daily reservations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DAILY_CAP_NANOUSD = 40_000_000
# Two requests, at most 4096 UTF-8 bytes each; reserve 8192 tokens each.
# Jev 1.13 input price: $0.042/M tokens; output is free. Never refund timeouts.
PAIR_RESERVATION_NANOUSD = 2 * 8192 * 42


class JevBudgetBoundaryError(ValueError):
    """Raised when a shadow pair must not start or continue past the UTC-day cutoff."""


def utc_budget_open(now: datetime | None = None) -> bool:
    """Whether a new reservation or Typesafe call is allowed at *now* (UTC)."""
    instant = now or datetime.now(UTC)
    return not (instant.hour == 23 and instant.minute == 59 and instant.second >= 50)


def digest(value: object) -> str:
    """Hash canonical JSON without retaining request text."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class JevReceiptStore:
    """One shared file per fork across processes; storage failures deny calls."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any], *, reserve: bool = False) -> bool:
        """Validate the chain and reserve budget atomically before network I/O."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            today = datetime.now(UTC).date().isoformat()
            spent = 0
            previous = None
            for line in stream:
                parsed = json.loads(line)
                claimed = parsed["receipt_hash"]
                row = {key: value for key, value in parsed.items() if key != "receipt_hash"}
                if row["previous_hash"] != previous or digest(row) != claimed:
                    raise ValueError("Invalid receipt chain")
                previous = claimed
                if row["day"] == today:
                    spent += row.get("reserved_nanousd", 0)
            if reserve and not utc_budget_open():
                return False
            if reserve and spent + PAIR_RESERVATION_NANOUSD > DAILY_CAP_NANOUSD:
                return False
            row = dict(
                record,
                day=today,
                timestamp_utc=datetime.now(UTC).isoformat(),
                previous_hash=previous,
            )
            row["receipt_hash"] = digest(row)
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return True

    def release_reservation(self, pair_id: str) -> None:
        """Undo a pair reservation when no Typesafe traffic was consumed."""
        self.append(
            {
                "kind": "reservation_release",
                "pair_id": pair_id,
                "reserved_nanousd": -PAIR_RESERVATION_NANOUSD,
            }
        )
