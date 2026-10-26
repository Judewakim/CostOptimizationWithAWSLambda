"""Async worker for the public API: assumes the visitor's read-only role and runs the scan."""
from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from ..aws import BOTO_CONFIG, assume_role_session
from ..scanner import scan_account

log = logging.getLogger()
log.setLevel(logging.INFO)

ROLE_ASSUME_HELP = ("Couldn't access your account. Make sure the WasteFinderReadOnly stack "
                    "finished creating in that account (status CREATE_COMPLETE), then try again.")


def handler(event, context):
    scan_id = event["scanId"]
    table = boto3.resource("dynamodb", config=BOTO_CONFIG).Table(os.environ["TABLE_NAME"])
    scan = table.get_item(Key={"pk": f"scan#{scan_id}"}).get("Item")
    if not scan or scan["status"] != "queued":
        return
    session = table.get_item(Key={"pk": f"session#{scan['sessionId']}"}).get("Item")
    if not session:
        _set(table, scan_id, "failed", error="Session expired. Start a new session.")
        return

    _set(table, scan_id, "running")
    try:
        target = assume_role_session(scan["accountId"], os.environ.get("ROLE_NAME", "WasteFinderReadOnly"),
                                     external_id=session["externalId"],
                                     session_name=f"waste-finder-{scan_id[:12]}")
    except ClientError:
        _set(table, scan_id, "failed", error=ROLE_ASSUME_HELP)
        return

    try:
        result = scan_account(target, scan["accountId"]).to_dict()
        boto3.client("s3", config=BOTO_CONFIG).put_object(
            Bucket=os.environ["RESULTS_BUCKET"], Key=f"results/{scan_id}.json",
            Body=json.dumps(result, default=str).encode(), ContentType="application/json")
        _set(table, scan_id, "done")
        log.info("scan %s done: $%.2f/month, %d findings", scan_id,
                 result["monthly_total"], len(result["findings"]))
    except Exception:  # noqa: BLE001
        log.exception("scan %s failed", scan_id)
        _set(table, scan_id, "failed", error="The scan hit an unexpected error. Try again shortly.")


def _set(table, scan_id: str, status: str, error: str | None = None) -> None:
    expr, values = "SET #s = :s", {":s": status}
    if error:
        expr += ", #e = :e"
        values[":e"] = error
    names = {"#s": "status", **({"#e": "error"} if error else {})}
    table.update_item(Key={"pk": f"scan#{scan_id}"}, UpdateExpression=expr,
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)
