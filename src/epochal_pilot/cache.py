"""On-disk response cache and a persistent Epochal request budget."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class Cache:
    """JSON files keyed by a hash of the request. Reruns hit the cache and cost nothing."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def key(*parts: object) -> str:
        blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def _path(self, ns: str, key: str) -> Path:
        return self.root / ns / f"{key}.json"

    def get(self, ns: str, key: str):
        p = self._path(ns, key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def has(self, ns: str, key: str) -> bool:
        return self._path(ns, key).exists()

    def put(self, ns: str, key: str, value) -> None:
        p = self._path(ns, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


class Budget:
    """Counts real (uncached) Epochal requests.

    * ``per_run``: max new requests in this run (the ``--budget`` flag).
    * ``max_total``: lifetime cap stored next to the cache (protects a free tier across runs).
    """

    def __init__(self, path: Path, per_run: int, max_total: int, verbose: bool = True):
        self.path, self.per_run, self.max_total, self.verbose = Path(path), per_run, max_total, verbose
        self.total = json.loads(self.path.read_text(encoding="utf-8"))["used"] if self.path.exists() else 0
        self.this_run = 0

    @property
    def remaining(self) -> int:
        return max(0, min(self.per_run - self.this_run, self.max_total - self.total))

    def spend(self) -> None:
        if self.this_run >= self.per_run:
            raise BudgetExceeded(f"--budget reached: {self.this_run}/{self.per_run} Epochal requests this run")
        if self.total >= self.max_total:
            raise BudgetExceeded(f"lifetime cap reached: {self.total}/{self.max_total} (see --max-total)")
        self.this_run += 1
        self.total += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"used": self.total}), encoding="utf-8")
        if self.verbose:
            print(
                f"  [epochal] request {self.this_run}/{self.per_run} this run, {self.total} lifetime",
                file=sys.stderr,
            )
