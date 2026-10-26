"""Rightsizing, sourced from AWS Compute Optimizer instead of a home-made CPU heuristic.

Compute Optimizer uses CPU, memory (when available), network and disk over 14+ days
and prices the recommended type. It's free, but each account has to opt in.
"""
from __future__ import annotations

from botocore.exceptions import ClientError, EndpointConnectionError

from ..aws import CheckContext
from ..models import ESTIMATE, Finding
from .base import ServiceNotEnabled, regional


@regional("ec2-rightsizing")
def overprovisioned_instances(ctx: CheckContext) -> list[Finding]:
    co = ctx.client("compute-optimizer")
    out, token = [], None
    while True:
        kwargs = {"nextToken": token} if token else {}
        try:
            resp = co.get_ec2_instance_recommendations(**kwargs)
        except EndpointConnectionError:
            return []  # Compute Optimizer isn't offered in this region
        except ClientError as e:
            if e.response["Error"]["Code"] == "OptInRequiredException":
                raise ServiceNotEnabled(
                    "AWS Compute Optimizer isn't turned on, so rightsizing was skipped. "
                    "It's free; enable it to include rightsizing in future scans.") from e
            raise
        for rec in resp.get("instanceRecommendations", []):
            if rec.get("finding", "").replace("_", "").lower() != "overprovisioned":
                continue
            options = sorted(rec.get("recommendationOptions", []), key=lambda o: o.get("rank", 99))
            if not options:
                continue
            best = options[0]
            saving = (best.get("savingsOpportunity", {})
                      .get("estimatedMonthlySavings", {}).get("value", 0.0))
            if not saving or saving <= 0:
                continue
            out.append(Finding(
                check="ec2-rightsizing",
                title="Over-provisioned EC2 instances",
                account_id=ctx.account_id, region=ctx.region,
                resource_id=rec["instanceArn"].rsplit("/", 1)[-1],
                resource_type="EC2 instance",
                monthly_cost=round(saving, 2), confidence=ESTIMATE,
                detail=(f"{rec.get('currentInstanceType')} is larger than its workload needs. "
                        f"Compute Optimizer recommends {best.get('instanceType')}."),
                action=f"Test on {best.get('instanceType')}, then change the instance type "
                       "during a maintenance window.",
            ))
        token = resp.get("nextToken")
        if not token:
            return out
