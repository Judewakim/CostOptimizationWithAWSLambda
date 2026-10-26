"""Synthesizes the CDK app and checks the security properties that matter."""
import json
import sys
from pathlib import Path

import pytest

aws_cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
from stacks import READ_ONLY_ACTIONS, DemoStack, ScannerStack  # noqa: E402



def _synth(stack_cls):
    app = aws_cdk.App(context={"notifyEmail": "a@b.co"})
    stack = stack_cls(app, "T", env=aws_cdk.Environment(account="111111111111", region="us-east-1"))
    return Template.from_stack(stack)


def test_scanner_permissions_are_read_only():
    for action in READ_ONLY_ACTIONS:
        verb = action.split(":")[1]
        assert verb.startswith(("Describe", "List", "Get")), action
    assert "s3:GetObject" not in READ_ONLY_ACTIONS
    assert not any(a.startswith("logs:Get") or a.startswith("logs:Filter") for a in READ_ONLY_ACTIONS)


def _template_actions():
    import re
    text = (ROOT / "cloudformation" / "readonly-role.yaml").read_text()
    return set(re.findall(r"\b([a-z0-9-]+:[A-Za-z0-9*]+)\b", text)) - {"sts:AssumeRole", "sts:ExternalId",
                                                                     "aws:PrincipalArn", "arn:aws"}


def test_role_template_matches_code_permissions():
    actions = _template_actions()
    assert actions == set(READ_ONLY_ACTIONS), actions ^ set(READ_ONLY_ACTIONS)
    for action in actions:
        assert action.split(":")[1].startswith(("Describe", "List", "Get")), action


def test_scanner_stack_buckets_block_public_access():
    t = _synth(ScannerStack)
    t.has_resource_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True}})
    t.resource_count_is("AWS::Events::Rule", 1)


def test_demo_stack_has_throttling_and_short_lived_results():
    t = _synth(DemoStack)
    t.has_resource_properties("AWS::ApiGatewayV2::Stage", {
        "DefaultRouteSettings": {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10}})
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "TimeToLiveSpecification": {"AttributeName": "expiresAt", "Enabled": True}})
    t.has_resource_properties("AWS::S3::Bucket", {
        "LifecycleConfiguration": {"Rules": Match.array_with([
            Match.object_like({"ExpirationInDays": 1})])}})


def test_only_the_role_template_is_public():
    t = _synth(DemoStack)
    public = [r for r in t.find_resources("AWS::S3::BucketPolicy").values()
              if '"AWS": "*"' in json.dumps(r) or '"*"' in json.dumps(r["Properties"]["PolicyDocument"]
                                                                   .get("Statement", [{}])[0].get("Principal", ""))]
    docs = json.dumps([r["Properties"]["PolicyDocument"] for r in public])
    assert "templates/readonly-role.yaml" in docs
