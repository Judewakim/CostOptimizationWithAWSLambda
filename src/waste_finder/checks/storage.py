"""Storage: abandoned S3 multipart uploads and log groups that never expire."""
from __future__ import annotations

from datetime import timedelta

from botocore.exceptions import ClientError

from ..aws import CheckContext
from ..models import ESTIMATE, UPPER_BOUND, Finding
from ..pricing import CLOUDWATCH_LOGS_GB_MONTH, GB, S3_STANDARD_GB_MONTH
from .base import global_check, regional

MPU_MIN_AGE = timedelta(days=7)
MPU_SAMPLE = 20                       # uploads per bucket to measure exactly
LOG_GROUP_MIN_BYTES = int(0.5 * GB)   # ignore tiny log groups


def bucket_region(s3, bucket: str) -> str:
    loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    if not loc:
        return "us-east-1"
    return "eu-west-1" if loc == "EU" else loc


@global_check("s3-stale-multipart")
def stale_multipart_uploads(ctx: CheckContext) -> list[Finding]:
    """Incomplete multipart uploads are billed as storage but invisible in the console."""
    s3 = ctx.client("s3", "us-east-1")
    out, skipped = [], 0
    for bucket in s3.list_buckets().get("Buckets", []):
        name = bucket["Name"]
        try:
            region = bucket_region(s3, name)
            rs3 = ctx.client("s3", region)
            stale = []
            for page in rs3.get_paginator("list_multipart_uploads").paginate(Bucket=name):
                stale += [u for u in page.get("Uploads", [])
                          if ctx.now - u["Initiated"] >= MPU_MIN_AGE]
            if not stale:
                continue
            measured = 0
            for u in stale[:MPU_SAMPLE]:
                parts = rs3.list_parts(Bucket=name, Key=u["Key"], UploadId=u["UploadId"])
                measured += sum(p["Size"] for p in parts.get("Parts", []))
            sampled = min(len(stale), MPU_SAMPLE)
            total_bytes = measured * len(stale) / sampled
            cost = round(total_bytes / GB * S3_STANDARD_GB_MONTH, 2)
            if cost < 0.01:
                continue
        except ClientError:
            skipped += 1
            continue
        out.append(Finding(
            check="s3-stale-multipart",
            title="Abandoned S3 multipart uploads",
            account_id=ctx.account_id, region=region,
            resource_id=name, resource_type="S3 bucket",
            monthly_cost=cost,
            confidence=ESTIMATE if len(stale) > MPU_SAMPLE else UPPER_BOUND,
            detail=(f"{len(stale)} upload(s) started over 7 days ago and never finished, "
                    f"about {total_bytes / GB:.1f} GB. They don't show in the console but are billed."),
            action="Add a lifecycle rule: abort incomplete multipart uploads after 7 days.",
        ))
    if skipped:
        ctx.notices.append(f"{skipped} S3 bucket(s) could not be read (bucket policy or "
                           "permissions) and were skipped.")
    return out


@regional("logs-no-retention")
def never_expiring_log_groups(ctx: CheckContext) -> list[Finding]:
    out = []
    paginator = ctx.client("logs").get_paginator("describe_log_groups")
    for page in paginator.paginate():
        for lg in page["logGroups"]:
            if lg.get("retentionInDays") or lg.get("storedBytes", 0) < LOG_GROUP_MIN_BYTES:
                continue
            gb = lg["storedBytes"] / GB
            out.append(Finding(
                check="logs-no-retention",
                title="Log groups that never expire",
                account_id=ctx.account_id, region=ctx.region,
                resource_id=lg["logGroupName"], resource_type="CloudWatch log group",
                monthly_cost=round(gb * CLOUDWATCH_LOGS_GB_MONTH, 2),
                confidence=UPPER_BOUND,
                detail=(f"{gb:.1f} GB stored with retention set to 'never expire', "
                        "so the bill grows every month."),
                action="Set a retention period (30-90 days is typical; longer if "
                       "compliance requires it). Saving is up to this amount.",
            ))
    return out
