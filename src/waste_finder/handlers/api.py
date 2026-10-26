"""Public "scan your own account" API behind API Gateway (HTTP API, payload v2).

Flow:
  POST /sessions          -> new session with a unique ExternalId and a one-click
                             CloudFormation link that creates a read-only role
  POST /scans             -> {sessionId, accountId}; queues a scan (async worker)
  GET  /scans/{scanId}    -> status, and the results once finished
                             (header X-Session-Id must match the session that started it)

Security:
  - The visitor's role trusts only the worker's role ARN, and only with this
    session's ExternalId (confused-deputy protection).
  - The role is read-only (see cloudformation/readonly-role.yaml).
  - Sessions and results expire automatically (DynamoDB TTL, S3 lifecycle).
  - A session can run at most MAX_SCANS_PER_SESSION scans.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
import uuid
from urllib.parse import urlencode

import boto3
from botocore.exceptions import ClientError

from ..aws import BOTO_CONFIG, validate_account_id

log = logging.getLogger()
log.setLevel(logging.INFO)

SESSION_TTL = 2 * 3600
MAX_SCANS_PER_SESSION = 3
MAX_BODY = 2048

_ddb = boto3.resource("dynamodb", config=BOTO_CONFIG)
_s3 = boto3.client("s3", config=BOTO_CONFIG)
_lambda = boto3.client("lambda", config=BOTO_CONFIG)


def _table():
    return _ddb.Table(os.environ["TABLE_NAME"])


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json",
                                              "Cache-Control": "no-store"},
            "body": json.dumps(body, default=str)}


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if len(raw) > MAX_BODY:
        raise ValueError("Request too large")
    if event.get("isBase64Encoded"):
        raise ValueError("Unsupported encoding")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Body must be a JSON object")
    return data


def quick_create_url(external_id: str) -> str:
    params = {
        "templateURL": os.environ["TEMPLATE_URL"],
        "stackName": "WasteFinderReadOnly",
        "param_ExternalId": external_id,
        "param_ScannerRoleArn": os.environ["SCANNER_ROLE_ARN"],
    }
    return ("https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?"
            + urlencode(params))


def create_session(_event: dict) -> dict:
    session_id = secrets.token_urlsafe(24)
    external_id = "wf-" + secrets.token_hex(16)
    expires = int(time.time()) + SESSION_TTL
    _table().put_item(Item={"pk": f"session#{session_id}", "externalId": external_id,
                            "scansUsed": 0, "expiresAt": expires})
    return _resp(201, {"sessionId": session_id, "externalId": external_id,
                       "expiresAt": expires, "launchStackUrl": quick_create_url(external_id),
                       "scannerRoleArn": os.environ["SCANNER_ROLE_ARN"]})


def start_scan(event: dict) -> dict:
    data = _body(event)
    session_id = str(data.get("sessionId", ""))[:64]
    account_id = validate_account_id(data.get("accountId", ""))
    table = _table()
    now = int(time.time())
    try:
        table.update_item(
            Key={"pk": f"session#{session_id}"},
            UpdateExpression="SET scansUsed = scansUsed + :one",
            ConditionExpression="attribute_exists(pk) AND expiresAt > :now AND scansUsed < :max",
            ExpressionAttributeValues={":one": 1, ":now": now, ":max": MAX_SCANS_PER_SESSION},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _resp(403, {"error": "Session expired or scan limit reached. "
                                        "Start a new session."})
        raise
    scan_id = uuid.uuid4().hex
    table.put_item(Item={"pk": f"scan#{scan_id}", "sessionId": session_id,
                         "accountId": account_id, "status": "queued",
                         "expiresAt": now + SESSION_TTL})
    _lambda.invoke(FunctionName=os.environ["WORKER_FUNCTION"], InvocationType="Event",
                   Payload=json.dumps({"scanId": scan_id}).encode())
    return _resp(202, {"scanId": scan_id, "status": "queued"})


def get_scan(event: dict) -> dict:
    scan_id = (event.get("pathParameters") or {}).get("scanId", "")
    session_id = (event.get("headers") or {}).get("x-session-id", "")
    if not (scan_id.isalnum() and len(scan_id) == 32):
        return _resp(404, {"error": "Not found"})
    item = _table().get_item(Key={"pk": f"scan#{scan_id}"}).get("Item")
    if not item or not hmac.compare_digest(item["sessionId"], session_id):
        return _resp(404, {"error": "Not found"})
    out = {"scanId": scan_id, "status": item["status"], "accountId": item["accountId"]}
    if item["status"] == "failed":
        out["error"] = item.get("error", "Scan failed")
    if item["status"] == "done":
        obj = _s3.get_object(Bucket=os.environ["RESULTS_BUCKET"], Key=f"results/{scan_id}.json")
        out["result"] = json.loads(obj["Body"].read())
    return _resp(200, out)


ROUTES = {
    "POST /sessions": create_session,
    "POST /scans": start_scan,
    "GET /scans/{scanId}": get_scan,
}


def handler(event, context):
    route = ROUTES.get(event.get("routeKey", ""))
    if not route:
        return _resp(404, {"error": "Not found"})
    try:
        return route(event)
    except (ValueError, json.JSONDecodeError) as err:
        return _resp(400, {"error": str(err) if isinstance(err, ValueError) else "Invalid JSON"})
    except Exception:  # noqa: BLE001
        log.exception("unhandled error on %s", event.get("routeKey"))
        return _resp(500, {"error": "Internal error"})
