"""按量计费后端的用量账本：每次调用一行 JSON，按自然月汇总花费。"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path


class UsageLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(
        self,
        *,
        backend: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        purpose: str,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        line = json.dumps(
            {
                "ts": now.astimezone(UTC).isoformat(timespec="seconds"),
                "backend": backend,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
                "purpose": purpose,
            },
            ensure_ascii=False,
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")

    def month_cost(self, backend: str, now: datetime | None = None) -> float:
        month = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        if not self._path.exists():
            return 0.0
        total = 0.0
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("backend") == backend and str(row.get("ts", "")).startswith(month):
                    total += float(row.get("cost_usd", 0))
        return total


def estimate_cost(
    input_tokens: int, output_tokens: int, price_in: float, price_out: float
) -> float:
    """价格单位：美元 / 百万 token。"""
    return input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out
