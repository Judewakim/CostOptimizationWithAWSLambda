from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

# How much to trust the dollar figure on a finding.
EXACT = "exact"              # fixed list price, e.g. an unused Elastic IP
ESTIMATE = "estimate"        # derived from usage or AWS's own recommendation
UPPER_BOUND = "upper-bound"  # real saving is this or less (e.g. incremental snapshots)


@dataclass
class Finding:
    check: str
    title: str
    account_id: str
    region: str
    resource_id: str
    resource_type: str
    monthly_cost: float
    confidence: str
    detail: str
    action: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckError:
    check: str
    region: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    account_id: str
    started_at: datetime
    finished_at: datetime | None = None
    regions: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[CheckError] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def monthly_total(self) -> float:
        return round(sum(f.monthly_cost for f in self.findings), 2)

    def by_check(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for f in self.findings:
            row = out.setdefault(f.check, {"title": f.title, "count": 0, "monthly_cost": 0.0})
            row["count"] += 1
            row["monthly_cost"] = round(row["monthly_cost"] + f.monthly_cost, 2)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["monthly_cost"]))

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "regions": self.regions,
            "monthly_total": self.monthly_total,
            "annual_total": round(self.monthly_total * 12, 2),
            "summary": self.by_check(),
            "findings": [f.to_dict() for f in
                         sorted(self.findings, key=lambda f: -f.monthly_cost)],
            "errors": [e.to_dict() for e in self.errors],
            "notices": self.notices,
        }
