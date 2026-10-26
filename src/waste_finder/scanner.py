"""Runs every check across every enabled region of one account, in parallel.

A failing check never stops the scan: the failure is recorded and the rest continue.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .aws import CheckContext, enabled_regions, utcnow
from .checks import GLOBAL, REGIONAL, ServiceNotEnabled
from .models import CheckError, ScanResult

log = logging.getLogger(__name__)

ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                 "UnauthorizedAccess", "AuthFailure"}
REGION_DISABLED = {"OptInRequired", "UnrecognizedClientException", "InvalidClientTokenId"}


def _describe(err: Exception) -> str | None:
    """Turn an exception into a short, safe message. None means 'ignore'."""
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "ClientError")
        if code in ACCESS_DENIED:
            return "Access denied: the scanning role is missing a permission for this check."
        if code in REGION_DISABLED:
            return None
        return f"{code}: {err.response.get('Error', {}).get('Message', '')}"[:300]
    if isinstance(err, BotoCoreError):
        return f"{type(err).__name__}: {err}"[:300]
    return f"Unexpected error ({type(err).__name__})"


def scan_account(session: boto3.session.Session, account_id: str,
                 regions: list[str] | None = None, checks: list[str] | None = None,
                 now: datetime | None = None, max_workers: int = 12) -> ScanResult:
    now = now or utcnow()
    result = ScanResult(account_id=account_id, started_at=now)
    result.regions = regions or enabled_regions(session)

    selected = set(checks) if checks else None
    jobs = [(cid, fn, region) for region in result.regions
            for cid, fn in REGIONAL.items() if not selected or cid in selected]
    jobs += [(cid, fn, "global") for cid, fn in GLOBAL.items() if not selected or cid in selected]

    notices: set[str] = set()

    def run(cid, fn, region):
        ctx = CheckContext(session=session, account_id=account_id,
                           region="us-east-1" if region == "global" else region, now=now)
        try:
            return cid, region, fn(ctx), None, ctx.notices
        except Exception as err:  # noqa: BLE001 - isolate every check
            return cid, region, [], err, ctx.notices

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run, *job) for job in jobs]
        for fut in as_completed(futures):
            cid, region, findings, err, check_notices = fut.result()
            result.findings.extend(findings)
            notices.update(check_notices)
            if err is None:
                continue
            if isinstance(err, ServiceNotEnabled):
                notices.add(str(err))
                continue
            message = _describe(err)
            if message:
                log.warning("check %s failed in %s: %s", cid, region, message)
                result.errors.append(CheckError(check=cid, region=region, message=message))

    result.notices = sorted(notices)
    result.errors.sort(key=lambda e: (e.check, e.region))
    result.finished_at = utcnow()
    return result
