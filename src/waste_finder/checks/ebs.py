"""EBS: unattached volumes, gp2 volumes that should be gp3, orphaned snapshots."""
from __future__ import annotations

from datetime import timedelta

from ..aws import CheckContext
from ..models import EXACT, UPPER_BOUND, Finding
from ..pricing import EBS_SNAPSHOT_GB_MONTH, ebs_volume_monthly, gp2_to_gp3_monthly_savings
from .base import regional

UNATTACHED_GRACE = timedelta(days=1)
SNAPSHOT_MIN_AGE = timedelta(days=90)
AWS_BACKUP_MARKER = "created by the AWS Backup service"


def _volumes(ctx: CheckContext, filters: list[dict] | None = None):
    paginator = ctx.client("ec2").get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=filters or []):
        yield from page["Volumes"]


def _name(tags: list[dict] | None) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


@regional("ebs-unattached")
def unattached_volumes(ctx: CheckContext) -> list[Finding]:
    out = []
    for v in _volumes(ctx, [{"Name": "status", "Values": ["available"]}]):
        if ctx.now - v["CreateTime"] < UNATTACHED_GRACE:
            continue
        cost = ebs_volume_monthly(v["VolumeType"], v["Size"], v.get("Iops"), v.get("Throughput"))
        age = (ctx.now - v["CreateTime"]).days
        label = _name(v.get("Tags"))
        out.append(Finding(
            check="ebs-unattached",
            title="Unattached EBS volumes",
            account_id=ctx.account_id, region=ctx.region,
            resource_id=v["VolumeId"], resource_type="EBS volume",
            monthly_cost=cost, confidence=EXACT,
            detail=(f"{v['Size']} GiB {v['VolumeType']} volume{f' ({label})' if label else ''} "
                    f"is not attached to any instance. Created {age} days ago."),
            action="Snapshot it if the data might be needed, then delete the volume.",
        ))
    return out


@regional("ebs-gp2")
def gp2_volumes(ctx: CheckContext) -> list[Finding]:
    out = []
    # Unattached gp2 volumes are already reported by ebs-unattached.
    for v in _volumes(ctx, [{"Name": "volume-type", "Values": ["gp2"]},
                            {"Name": "status", "Values": ["in-use"]}]):
        saving = gp2_to_gp3_monthly_savings(v["Size"])
        if saving <= 0:
            continue
        out.append(Finding(
            check="ebs-gp2",
            title="gp2 volumes that should be gp3",
            account_id=ctx.account_id, region=ctx.region,
            resource_id=v["VolumeId"], resource_type="EBS volume",
            monthly_cost=saving, confidence=EXACT,
            detail=(f"{v['Size']} GiB gp2 volume. gp3 is about 20% cheaper per GB "
                    "with the same or better baseline performance."),
            action="Modify the volume type to gp3 (online, no downtime). "
                   "Match IOPS if the volume is larger than 1 TiB.",
        ))
    return out


@regional("ebs-snapshot-orphaned")
def orphaned_snapshots(ctx: CheckContext) -> list[Finding]:
    ec2 = ctx.client("ec2")
    live_volumes = {v["VolumeId"] for v in _volumes(ctx)}
    ami_snapshots = set()
    for page in ec2.get_paginator("describe_images").paginate(Owners=["self"]):
        for img in page.get("Images", []):
            for bdm in img.get("BlockDeviceMappings", []):
                snap = bdm.get("Ebs", {}).get("SnapshotId")
                if snap:
                    ami_snapshots.add(snap)

    out = []
    paginator = ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            if s["VolumeId"] in live_volumes or s["SnapshotId"] in ami_snapshots:
                continue
            if AWS_BACKUP_MARKER in (s.get("Description") or ""):
                continue  # retention is owned by an AWS Backup plan
            age = ctx.now - s["StartTime"]
            if age < SNAPSHOT_MIN_AGE:
                continue
            out.append(Finding(
                check="ebs-snapshot-orphaned",
                title="Old snapshots of deleted volumes",
                account_id=ctx.account_id, region=ctx.region,
                resource_id=s["SnapshotId"], resource_type="EBS snapshot",
                monthly_cost=round(s["VolumeSize"] * EBS_SNAPSHOT_GB_MONTH, 2),
                confidence=UPPER_BOUND,
                detail=(f"Snapshot of a {s['VolumeSize']} GiB volume that no longer exists, "
                        f"{age.days} days old, not used by any of your AMIs. Snapshots are "
                        "incremental, so the real cost is this amount or less."),
                action="Confirm nobody needs it for recovery or compliance, then delete it "
                       "(or move it to the snapshot archive tier).",
            ))
    return out
