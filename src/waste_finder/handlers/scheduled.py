"""Scheduled scan: EventBridge -> this Lambda -> report in S3 -> summary to SNS.

Environment:
  REPORT_BUCKET          S3 bucket for reports (required)
  SNS_TOPIC_ARN          topic for the email summary (optional)
  TARGET_ACCOUNT_IDS     comma-separated account IDs to scan (default: this account)
  ROLE_NAME              read-only role to assume in other accounts (default WasteFinderReadOnly)
  EXTERNAL_ID_PARAMETER  SSM SecureString holding the ExternalId for that role (optional)
  SCAN_REGIONS           comma-separated regions to limit the scan (default: all enabled)
"""
from __future__ import annotations

import logging
import os
import uuid
from urllib.parse import quote

import boto3

from ..aws import BOTO_CONFIG, assume_role_session, current_account_id, utcnow, validate_account_id
from ..models import CheckError, ScanResult
from ..report import build_report, email_summary, render_html, to_json
from ..scanner import scan_account

log = logging.getLogger()
log.setLevel(logging.INFO)


def _target_accounts(event: dict, own_account: str) -> list[str]:
    raw = event.get("account_ids") or os.environ.get("TARGET_ACCOUNT_IDS", "")
    if isinstance(raw, str):
        raw = [a for a in raw.split(",") if a.strip()]
    accounts = [validate_account_id(a) for a in raw] or [own_account]
    return list(dict.fromkeys(accounts))  # de-duplicate, keep order


def _regions() -> list[str] | None:
    raw = os.environ.get("SCAN_REGIONS", "")
    return [r.strip() for r in raw.split(",") if r.strip()] or None


def _external_id() -> str | None:
    name = os.environ.get("EXTERNAL_ID_PARAMETER")
    if not name:
        return None
    ssm = boto3.client("ssm", config=BOTO_CONFIG)
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def handler(event, context):
    bucket = os.environ["REPORT_BUCKET"]
    role_name = os.environ.get("ROLE_NAME", "WasteFinderReadOnly")
    own_account = current_account_id()
    accounts = _target_accounts(event or {}, own_account)
    external_id = _external_id() if any(a != own_account for a in accounts) else None

    scans = []
    for account_id in accounts:
        try:
            session = (boto3.session.Session() if account_id == own_account else
                       assume_role_session(account_id, role_name, external_id))
            result = scan_account(session, account_id, regions=_regions())
        except Exception as err:  # noqa: BLE001 - one bad account must not sink the run
            log.error("account %s could not be scanned: %s", account_id, type(err).__name__)
            result = ScanResult(account_id=account_id, started_at=utcnow(), finished_at=utcnow())
            result.errors.append(CheckError(
                check="assume-role", region="global",
                message=f"Could not access the account ({type(err).__name__}). "
                        f"Check the {role_name} role and its trust policy."))
        scans.append(result.to_dict())
        log.info("account %s: $%.2f/month across %d findings",
                 account_id, result.monthly_total, len(result.findings))

    now = utcnow()
    report = build_report(scans, now)
    prefix = f"reports/{now:%Y/%m/%d}/run-{uuid.uuid4().hex[:12]}"
    s3 = boto3.client("s3", config=BOTO_CONFIG)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/report.json", Body=to_json(report).encode(),
                  ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=f"{prefix}/report.html", Body=render_html(report).encode(),
                  ContentType="text/html; charset=utf-8")

    topic = os.environ.get("SNS_TOPIC_ARN")
    if topic:
        region = os.environ.get("AWS_REGION", "us-east-1")
        link = (f"https://{region}.console.aws.amazon.com/s3/object/{bucket}"
                f"?region={region}&prefix={quote(prefix + '/report.html')}")
        boto3.client("sns", config=BOTO_CONFIG).publish(
            TopicArn=topic,
            Subject=f"AWS waste: ${report['monthly_total']:,.0f}/month found"[:100],
            Message=email_summary(report, link),
        )

    return {"report_prefix": prefix, "monthly_total": report["monthly_total"],
            "accounts": len(scans)}
