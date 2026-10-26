"""EC2: unused Elastic IPs and long-stopped instances still paying for storage."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..aws import CheckContext
from ..models import EXACT, Finding
from ..pricing import PUBLIC_IPV4_HOUR, ebs_volume_monthly, monthly
from .base import regional

STOPPED_MIN_AGE = timedelta(days=30)
_STOP_TIME_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)\)")


@regional("eip-unused")
def unused_elastic_ips(ctx: CheckContext) -> list[Finding]:
    out = []
    for a in ctx.client("ec2").describe_addresses().get("Addresses", []):
        if a.get("AssociationId") or a.get("NetworkInterfaceId"):
            continue
        out.append(Finding(
            check="eip-unused",
            title="Unused Elastic IPs",
            account_id=ctx.account_id, region=ctx.region,
            resource_id=a.get("AllocationId") or a.get("PublicIp", "unknown"),
            resource_type="Elastic IP",
            monthly_cost=monthly(PUBLIC_IPV4_HOUR), confidence=EXACT,
            detail=f"{a.get('PublicIp')} is allocated but not attached to anything. "
                   "AWS bills every public IPv4 address by the hour.",
            action="Release the address if nothing depends on it (DNS records, allow-lists).",
        ))
    return out


def stopped_since(reason: str | None) -> datetime | None:
    """Parse the stop time out of an instance's StateTransitionReason."""
    m = _STOP_TIME_RE.search(reason or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


@regional("ec2-stopped")
def long_stopped_instances(ctx: CheckContext) -> list[Finding]:
    ec2 = ctx.client("ec2")
    stopped = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                since = stopped_since(inst.get("StateTransitionReason"))
                if since and ctx.now - since >= STOPPED_MIN_AGE:
                    stopped.append((inst, since))
    if not stopped:
        return []

    volume_ids = [bdm["Ebs"]["VolumeId"] for inst, _ in stopped
                  for bdm in inst.get("BlockDeviceMappings", []) if "Ebs" in bdm]
    volumes = {}
    for i in range(0, len(volume_ids), 200):
        for v in ec2.describe_volumes(VolumeIds=volume_ids[i:i + 200])["Volumes"]:
            volumes[v["VolumeId"]] = v

    out = []
    for inst, since in stopped:
        vols = [volumes[b["Ebs"]["VolumeId"]] for b in inst.get("BlockDeviceMappings", [])
                if "Ebs" in b and b["Ebs"]["VolumeId"] in volumes]
        cost = round(sum(ebs_volume_monthly(v["VolumeType"], v["Size"], v.get("Iops"),
                                            v.get("Throughput")) for v in vols), 2)
        if cost <= 0:
            continue
        total_gb = sum(v["Size"] for v in vols)
        out.append(Finding(
            check="ec2-stopped",
            title="Instances stopped for 30+ days",
            account_id=ctx.account_id, region=ctx.region,
            resource_id=inst["InstanceId"], resource_type="EC2 instance",
            monthly_cost=cost, confidence=EXACT,
            detail=(f"{inst.get('InstanceType')} stopped for {(ctx.now - since).days} days. "
                    f"Its {len(vols)} attached volume(s), {total_gb} GiB in total, are still billed."),
            action="If it's not coming back, create an AMI (or snapshots) and terminate it.",
        ))
    return out
