import os
import sys
from datetime import timedelta
from pathlib import Path

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Fake credentials so nothing can ever reach a real account.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
})

from moto import mock_aws  # noqa: E402

from waste_finder.aws import CheckContext, utcnow  # noqa: E402

REGION = "us-east-1"
ACCOUNT = "123456789012"


@pytest.fixture
def aws():
    with mock_aws():
        yield boto3.session.Session(region_name=REGION)


@pytest.fixture
def ctx_at(aws):
    """Build a CheckContext as if the scan ran `days` days from now."""
    def make(days: float = 0) -> CheckContext:
        return CheckContext(session=aws, account_id=ACCOUNT, region=REGION,
                            now=utcnow() + timedelta(days=days))
    return make


@pytest.fixture
def ec2(aws):
    return aws.client("ec2", region_name=REGION)


@pytest.fixture
def instance(ec2):
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    return ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1,
                             InstanceType="t3.micro")["Instances"][0]
