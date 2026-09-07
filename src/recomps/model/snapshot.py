"""Run snapshots (F12).

Every run writes a timestamped JSON file holding every row and every computed
statistic. Two things depend on that being complete rather than a summary:
``--compare`` diffs two snapshots, and a snapshot can be re-analyzed offline
without re-running any research.

Snapshots carry a schema version. A comparison across incompatible versions
fails loudly instead of silently diffing mismatched shapes -- a run-over-run
report that quietly compares the wrong fields is worse than no report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 1


class SnapshotIncompatible(ValueError):
    pass


@dataclass
class Snapshot:
    schema_version: int
    payload: dict[str, Any]

    @property
    def run_at(self) -> str:
        return self.payload.get("run", {}).get("run_at", "")

    @property
    def label(self) -> str:
        run = self.payload.get("run", {})
        return f"{run.get('market', '?')} {self.local_date}"

    @property
    def local_date(self) -> str:
        """The stored stamp is UTC; a reader wants the day it happened here.

        An unparseable stamp falls back to its first ten characters rather than
        raising: a label is not worth failing a run over.
        """
        from datetime import datetime

        from recomps.clock import local_stamp

        try:
            return local_stamp(datetime.fromisoformat(self.run_at), "%Y-%m-%d")
        except ValueError:
            return self.run_at[:10]

    def to_json(self) -> str:
        return json.dumps(
            {"schema_version": self.schema_version, **self.payload},
            indent=2,
            sort_keys=False,
            default=_json_default,
        )

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> Snapshot:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        version = raw.pop("schema_version", 0)
        if version != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotIncompatible(
                f"{path} was written by snapshot schema v{version}; this build reads "
                f"v{SNAPSHOT_SCHEMA_VERSION}. Re-run the pipeline to regenerate it."
            )
        return cls(schema_version=version, payload=raw)


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):  # Enum
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def build_snapshot(result: Any) -> Snapshot:
    """Serialize a RunResult. Imported lazily to keep the model layer standalone."""
    payload = {
        "run": {
            "market": result.market_name,
            "market_description": result.market_description,
            "profile": result.profile.to_dict(),
            "run_at": result.run_at.isoformat(),
            "window_start": result.window_start.isoformat(),
            "window_end": result.window_end.isoformat(),
            "researcher": result.researcher,
        },
        "sold": [c.to_dict() for c in result.sold],
        "active": [c.to_dict() for c in result.active],
        "stats": {
            "sold": result.sold_stats.to_dict(),
            "active": result.active_stats.to_dict(),
            "sold_to_ask": result.sold_to_ask.to_dict(),
        },
        "valuation": result.valuation.to_dict() if result.valuation else None,
        "guidance": result.guidance.to_dict() if result.guidance else None,
        "areas": result.area_table.to_dict(),
        "size_bands": result.size_bands.to_dict(),
        "ladder": result.ladder.to_dict(),
        "agents": result.agent_analysis.to_dict(),
        "excluded": [{"address": e.address, "reason": e.reason} for e in result.excluded],
        "reconciled": result.reconciled,
        "missing_metric": result.missing_metric,
        "caveats": result.caveats,
        "warnings": result.warnings,
        "diagnostics": result.diagnostics.to_dict(),
    }
    return Snapshot(schema_version=SNAPSHOT_SCHEMA_VERSION, payload=payload)


def snapshot_filename(result: Any) -> str:
    stamp = result.run_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{result.market_name}-{result.profile.name}-{stamp}.snapshot.json"
