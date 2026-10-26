"""AWS session helpers: cross-account role assumption and region discovery."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
from botocore.config import Config

ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
ROLE_NAME_RE = re.compile(r"^[\w+=,.@-]{1,64}$")

BOTO_CONFIG = Config(
    retries={"max_attempts": 8, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=30,
    user_agent_extra="aws-waste-finder/2.0",
)


# boto3 sessions are not thread-safe when creating clients; clients themselves are.
_CLIENT_LOCK = threading.Lock()


@dataclass
class CheckContext:
    """Everything a check needs to run against one account in one region."""
    session: boto3.session.Session
    account_id: str
    region: str
    now: datetime
    notices: list[str] = field(default_factory=list)

    def client(self, service: str, region: str | None = None):
        with _CLIENT_LOCK:
            return self.session.client(service, region_name=region or self.region,
                                       config=BOTO_CONFIG)


def validate_account_id(account_id: str) -> str:
    account_id = str(account_id).strip()
    if not ACCOUNT_ID_RE.match(account_id):
        raise ValueError("Account ID must be exactly 12 digits")
    return account_id


def current_account_id(session: boto3.session.Session | None = None) -> str:
    session = session or boto3.session.Session()
    return session.client("sts", config=BOTO_CONFIG).get_caller_identity()["Account"]


def assume_role_session(account_id: str, role_name: str, external_id: str | None = None,
                        session_name: str = "aws-waste-finder",
                        base_session: boto3.session.Session | None = None,
                        duration_seconds: int = 3600) -> boto3.session.Session:
    """Return a boto3 session in the target account using a read-only role."""
    account_id = validate_account_id(account_id)
    if not ROLE_NAME_RE.match(role_name):
        raise ValueError("Invalid role name")
    base_session = base_session or boto3.session.Session()
    params = {
        "RoleArn": f"arn:aws:iam::{account_id}:role/{role_name}",
        "RoleSessionName": session_name,
        "DurationSeconds": duration_seconds,
    }
    if external_id:
        params["ExternalId"] = external_id
    creds = base_session.client("sts", config=BOTO_CONFIG).assume_role(**params)["Credentials"]
    return boto3.session.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def enabled_regions(session: boto3.session.Session, home_region: str = "us-east-1") -> list[str]:
    """Regions enabled for this account (opt-in regions only if opted in)."""
    ec2 = session.client("ec2", region_name=home_region, config=BOTO_CONFIG)
    regions = ec2.describe_regions(AllRegions=False)["Regions"]
    return sorted(r["RegionName"] for r in regions)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
