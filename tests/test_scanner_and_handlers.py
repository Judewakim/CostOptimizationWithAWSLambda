import json

import boto3
import pytest
from botocore.exceptions import ClientError

from waste_finder.checks import base
from waste_finder.checks.base import ServiceNotEnabled
from waste_finder.report import build_report, email_summary, render_html
from waste_finder.scanner import scan_account

from conftest import ACCOUNT, REGION


@pytest.fixture
def isolated_checks(monkeypatch):
    """Swap the real registry for a controlled one."""
    regional, global_ = {}, {}
    monkeypatch.setattr(base, "REGIONAL", regional)
    monkeypatch.setattr(base, "GLOBAL", global_)
    import waste_finder.scanner as scanner_mod
    monkeypatch.setattr(scanner_mod, "REGIONAL", regional)
    monkeypatch.setattr(scanner_mod, "GLOBAL", global_)
    return regional, global_


def test_one_failing_check_does_not_stop_the_scan(aws, ec2, isolated_checks):
    regional, _ = isolated_checks
    from waste_finder.checks.ec2 import unused_elastic_ips

    def broken(ctx):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "Describe")

    def not_enabled(ctx):
        raise ServiceNotEnabled("turn it on")

    regional.update({"eip-unused": unused_elastic_ips, "broken": broken, "off": not_enabled})
    ec2.allocate_address(Domain="vpc")

    result = scan_account(aws, ACCOUNT, regions=[REGION, "us-west-2"])
    assert len(result.findings) == 1                     # us-east-1 EIP still found
    assert {e.region for e in result.errors} == {REGION, "us-west-2"}
    assert "missing a permission" in result.errors[0].message
    assert result.notices == ["turn it on"]               # reported once, not per region


def test_full_scan_and_reports(aws, ec2):
    ec2.allocate_address(Domain="vpc")
    ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=100, VolumeType="gp2")
    result = scan_account(aws, ACCOUNT, regions=[REGION])
    data = result.to_dict()
    assert data["monthly_total"] >= 3.65
    report = build_report([data], result.started_at)
    html = render_html(report)
    assert "Unused Elastic IPs" in html and "<script" not in html
    text = email_summary(report, "https://console.example")
    assert "eipalloc" not in text  # no resource IDs in email


def test_html_report_escapes_resource_names():
    report = build_report([{
        "account_id": ACCOUNT, "monthly_total": 1.0, "summary": {}, "errors": [], "notices": [],
        "findings": [{"account_id": ACCOUNT, "region": REGION, "title": "t",
                      "resource_id": "<img src=x onerror=alert(1)>", "monthly_cost": 1.0,
                      "confidence": "exact", "detail": "d", "action": "a"}]}],
        __import__("datetime").datetime(2026, 1, 1))
    assert "<img" not in render_html(report)


def test_scheduled_handler_writes_report_and_emails(aws, ec2, monkeypatch):
    from waste_finder.handlers import scheduled
    s3, sns = aws.client("s3"), aws.client("sns")
    s3.create_bucket(Bucket="reports-bucket")
    topic = sns.create_topic(Name="waste")["TopicArn"]
    monkeypatch.setenv("REPORT_BUCKET", "reports-bucket")
    monkeypatch.setenv("SNS_TOPIC_ARN", topic)
    monkeypatch.setenv("SCAN_REGIONS", REGION)
    ec2.allocate_address(Domain="vpc")

    out = scheduled.handler({}, None)
    assert out["accounts"] == 1 and out["monthly_total"] >= 3.65
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="reports-bucket")["Contents"]]
    assert any(k.endswith("report.html") for k in keys)
    assert any(k.endswith("report.json") for k in keys)


def test_scheduled_handler_rejects_bad_account_ids(aws, monkeypatch):
    from waste_finder.handlers import scheduled
    monkeypatch.setenv("REPORT_BUCKET", "x")
    with pytest.raises(ValueError):
        scheduled.handler({"account_ids": ["12345; rm -rf"]}, None)


def test_unreachable_account_is_reported_not_fatal(aws, monkeypatch):
    from waste_finder.handlers import scheduled
    aws.client("s3").create_bucket(Bucket="reports-bucket")
    monkeypatch.setenv("REPORT_BUCKET", "reports-bucket")
    monkeypatch.setenv("SCAN_REGIONS", REGION)

    def boom(*a, **k):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "AssumeRole")
    monkeypatch.setattr(scheduled, "assume_role_session", boom)
    out = scheduled.handler({"account_ids": ["111111111111", ACCOUNT]}, None)
    assert out["accounts"] == 2


# ---------- public API ----------

@pytest.fixture
def api(aws, monkeypatch):
    ddb = aws.client("dynamodb")
    ddb.create_table(TableName="wf", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                     BillingMode="PAY_PER_REQUEST")
    aws.client("s3").create_bucket(Bucket="results-bucket")
    monkeypatch.setenv("TABLE_NAME", "wf")
    monkeypatch.setenv("RESULTS_BUCKET", "results-bucket")
    monkeypatch.setenv("WORKER_FUNCTION", "worker")
    monkeypatch.setenv("TEMPLATE_URL", "https://example.s3.amazonaws.com/readonly-role.yaml")
    monkeypatch.setenv("SCANNER_ROLE_ARN", "arn:aws:iam::999999999999:role/worker")
    from waste_finder.handlers import api as api_mod
    monkeypatch.setattr(api_mod, "_ddb", boto3.resource("dynamodb", region_name=REGION))
    monkeypatch.setattr(api_mod, "_s3", boto3.client("s3", region_name=REGION))
    invoked = []

    class FakeLambda:
        def invoke(self, **kw):
            invoked.append(json.loads(kw["Payload"]))
    monkeypatch.setattr(api_mod, "_lambda", FakeLambda())
    api_mod.invoked = invoked
    return api_mod


def call(api, route, body=None, path=None, headers=None):
    resp = api.handler({"routeKey": route, "body": json.dumps(body) if body else None,
                        "pathParameters": path or {}, "headers": headers or {}}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_session_returns_launch_link_with_external_id(api):
    status, body = call(api, "POST /sessions")
    assert status == 201
    assert body["externalId"] in body["launchStackUrl"]
    assert "quickcreate" in body["launchStackUrl"]


def test_scan_flow_and_session_isolation(api):
    _, sess = call(api, "POST /sessions")
    status, scan = call(api, "POST /scans", {"sessionId": sess["sessionId"], "accountId": ACCOUNT})
    assert status == 202 and api.invoked == [{"scanId": scan["scanId"]}]

    status, _ = call(api, "GET /scans/{scanId}", path={"scanId": scan["scanId"]},
                     headers={"x-session-id": "someone-else"})
    assert status == 404  # another visitor can't read your results
    status, got = call(api, "GET /scans/{scanId}", path={"scanId": scan["scanId"]},
                       headers={"x-session-id": sess["sessionId"]})
    assert status == 200 and got["status"] == "queued"


def test_scan_limit_per_session(api):
    _, sess = call(api, "POST /sessions")
    for _ in range(3):
        assert call(api, "POST /scans", {"sessionId": sess["sessionId"], "accountId": ACCOUNT})[0] == 202
    assert call(api, "POST /scans", {"sessionId": sess["sessionId"], "accountId": ACCOUNT})[0] == 403


def test_bad_input_rejected(api):
    _, sess = call(api, "POST /sessions")
    assert call(api, "POST /scans", {"sessionId": sess["sessionId"], "accountId": "abc"})[0] == 400
    assert call(api, "POST /scans", {"sessionId": "nope", "accountId": ACCOUNT})[0] == 403
    assert call(api, "DELETE /everything")[0] == 404


def test_worker_completes_scan(api, monkeypatch):
    from waste_finder.handlers import worker
    from waste_finder.models import ScanResult
    from waste_finder.aws import utcnow
    _, sess = call(api, "POST /sessions")
    _, scan = call(api, "POST /scans", {"sessionId": sess["sessionId"], "accountId": ACCOUNT})

    seen = {}

    def fake_assume(account_id, role, external_id=None, **kw):
        seen["external_id"] = external_id
        return boto3.session.Session(region_name=REGION)
    monkeypatch.setattr(worker, "assume_role_session", fake_assume)
    monkeypatch.setattr(worker, "scan_account",
                        lambda s, a: ScanResult(account_id=a, started_at=utcnow(), finished_at=utcnow()))
    worker.handler({"scanId": scan["scanId"]}, None)

    assert seen["external_id"] == sess["externalId"]
    status, got = call(api, "GET /scans/{scanId}", path={"scanId": scan["scanId"]},
                       headers={"x-session-id": sess["sessionId"]})
    assert got["status"] == "done" and got["result"]["account_id"] == ACCOUNT
