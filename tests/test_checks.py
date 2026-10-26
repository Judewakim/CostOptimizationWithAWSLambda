from datetime import datetime, timezone

import pytest
from botocore.stub import Stubber

from waste_finder.checks import ebs, ec2 as ec2_checks, network, rightsizing, storage
from waste_finder.checks.base import ServiceNotEnabled
from waste_finder.models import EXACT, UPPER_BOUND

from conftest import REGION


# ---------- EBS ----------

def test_unattached_volume_found_with_exact_cost(ec2, ctx_at):
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=50, VolumeType="gp3")
    findings = ebs.unattached_volumes(ctx_at(days=3))
    assert [f.resource_id for f in findings] == [vol["VolumeId"]]
    assert findings[0].monthly_cost == 4.0
    assert findings[0].confidence == EXACT


def test_brand_new_unattached_volume_is_ignored(ec2, ctx_at):
    ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=50, VolumeType="gp3")
    assert ebs.unattached_volumes(ctx_at(days=0)) == []


def test_attached_gp2_volume_flagged_for_gp3(ec2, instance, ctx_at):
    vol = ec2.create_volume(AvailabilityZone=instance["Placement"]["AvailabilityZone"],
                            Size=100, VolumeType="gp2")
    ec2.attach_volume(VolumeId=vol["VolumeId"], InstanceId=instance["InstanceId"], Device="/dev/sdf")
    found = {f.resource_id: f for f in ebs.gp2_volumes(ctx_at())}
    assert found[vol["VolumeId"]].monthly_cost == 2.0


def test_unattached_gp2_not_double_counted(ec2, ctx_at):
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=100, VolumeType="gp2")
    assert vol["VolumeId"] not in {f.resource_id for f in ebs.gp2_volumes(ctx_at())}


def test_orphaned_snapshot_found_after_volume_deleted(ec2, ctx_at):
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=20)
    snap = ec2.create_snapshot(VolumeId=vol["VolumeId"], Description="nightly")
    ec2.delete_volume(VolumeId=vol["VolumeId"])
    found = {f.resource_id: f for f in ebs.orphaned_snapshots(ctx_at(days=100))}
    assert found[snap["SnapshotId"]].monthly_cost == 1.0
    assert found[snap["SnapshotId"]].confidence == UPPER_BOUND


def test_snapshot_of_live_volume_is_kept(ec2, ctx_at):
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=20)
    snap = ec2.create_snapshot(VolumeId=vol["VolumeId"])
    assert snap["SnapshotId"] not in {f.resource_id for f in ebs.orphaned_snapshots(ctx_at(days=100))}


def test_aws_backup_snapshots_are_left_alone(ec2, ctx_at):
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=20)
    snap = ec2.create_snapshot(VolumeId=vol["VolumeId"],
                               Description="This snapshot is created by the AWS Backup service.")
    ec2.delete_volume(VolumeId=vol["VolumeId"])
    assert snap["SnapshotId"] not in {f.resource_id for f in ebs.orphaned_snapshots(ctx_at(days=100))}


# ---------- EC2 ----------

def test_unused_elastic_ip(ec2, ctx_at):
    alloc = ec2.allocate_address(Domain="vpc")
    findings = ec2_checks.unused_elastic_ips(ctx_at())
    assert [f.resource_id for f in findings] == [alloc["AllocationId"]]
    assert findings[0].monthly_cost == 3.65


def test_attached_elastic_ip_ignored(ec2, instance, ctx_at):
    alloc = ec2.allocate_address(Domain="vpc")
    ec2.associate_address(AllocationId=alloc["AllocationId"], InstanceId=instance["InstanceId"])
    assert ec2_checks.unused_elastic_ips(ctx_at()) == []


def test_stop_time_parser():
    reason = "User initiated (2026-01-02 03:04:05 GMT)"
    assert ec2_checks.stopped_since(reason) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert ec2_checks.stopped_since("") is None


def test_long_stopped_instance_reports_storage_cost(ec2, instance, ctx_at):
    ec2.stop_instances(InstanceIds=[instance["InstanceId"]])
    findings = ec2_checks.long_stopped_instances(ctx_at(days=45))
    assert [f.resource_id for f in findings] == [instance["InstanceId"]]
    assert findings[0].monthly_cost > 0
    assert ec2_checks.long_stopped_instances(ctx_at(days=5)) == []


# ---------- Networking ----------

def _subnets(ec2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    a = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone=f"{REGION}a")
    b = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone=f"{REGION}b")
    return vpc, [a["Subnet"]["SubnetId"], b["Subnet"]["SubnetId"]]


def test_idle_nat_gateway(ec2, ctx_at):
    _, subnets = _subnets(ec2)
    alloc = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=alloc)["NatGateway"]
    findings = network.idle_nat_gateways(ctx_at(days=20))
    assert [f.resource_id for f in findings] == [nat["NatGatewayId"]]
    assert findings[0].monthly_cost == 32.85 + 3.65


def test_load_balancer_without_targets_is_idle(aws, ec2, ctx_at):
    _, subnets = _subnets(ec2)
    aws.client("elbv2").create_load_balancer(Name="orphan-alb", Subnets=subnets, Type="application")
    findings = network.idle_load_balancers(ctx_at(days=20))
    assert [(f.resource_id, f.resource_type, f.monthly_cost) for f in findings] == \
        [("orphan-alb", "ALB", 16.43)]


def test_classic_elb_without_instances_is_idle(aws, ctx_at):
    aws.client("elb").create_load_balancer(
        LoadBalancerName="old-clb", AvailabilityZones=[f"{REGION}a"],
        Listeners=[{"Protocol": "HTTP", "LoadBalancerPort": 80, "InstancePort": 80}])
    findings = network.idle_load_balancers(ctx_at(days=20))
    assert [f.resource_type for f in findings] == ["Classic ELB"]


# ---------- Storage ----------

def test_stale_multipart_upload(aws, ctx_at, monkeypatch):
    monkeypatch.setattr(storage, "S3_STANDARD_GB_MONTH", 1000.0)  # make 5 MB register
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="uploads-bucket")
    up = s3.create_multipart_upload(Bucket="uploads-bucket", Key="big.bin")
    s3.upload_part(Bucket="uploads-bucket", Key="big.bin", UploadId=up["UploadId"],
                   PartNumber=1, Body=b"x" * (5 * 1024 * 1024))
    findings = storage.stale_multipart_uploads(ctx_at(days=10))
    assert [f.resource_id for f in findings] == ["uploads-bucket"]
    assert findings[0].monthly_cost > 0
    # moto stamps every upload as initiated 2010-11-10; two days later it isn't stale yet.
    ctx = ctx_at()
    ctx.now = datetime(2010, 11, 12, tzinfo=timezone.utc)
    assert storage.stale_multipart_uploads(ctx) == []


class _StubCtx:
    """Context whose client() returns one stubbed client."""
    def __init__(self, client, now=None):
        self._client, self.account_id, self.region = client, "123456789012", REGION
        self.now = now or datetime.now(timezone.utc)
        self.notices = []

    def client(self, *_args, **_kw):
        return self._client


def test_log_group_without_retention(aws):
    logs = aws.client("logs")
    with Stubber(logs) as stub:
        stub.add_response("describe_log_groups", {"logGroups": [
            {"logGroupName": "/app/noisy", "storedBytes": 10 * 1024 ** 3},
            {"logGroupName": "/app/kept", "storedBytes": 10 * 1024 ** 3, "retentionInDays": 30},
            {"logGroupName": "/app/tiny", "storedBytes": 1024},
        ]})
        findings = storage.never_expiring_log_groups(_StubCtx(logs))
    assert [(f.resource_id, f.monthly_cost) for f in findings] == [("/app/noisy", 0.3)]


# ---------- Rightsizing ----------

def test_compute_optimizer_overprovisioned(aws):
    co = aws.client("compute-optimizer")
    with Stubber(co) as stub:
        stub.add_response("get_ec2_instance_recommendations", {"instanceRecommendations": [
            {"instanceArn": "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc",
             "currentInstanceType": "m5.2xlarge", "finding": "Overprovisioned",
             "recommendationOptions": [
                 {"instanceType": "m5.large", "rank": 1, "savingsOpportunity":
                     {"estimatedMonthlySavings": {"currency": "USD", "value": 210.24}}}]},
            {"instanceArn": "arn:aws:ec2:us-east-1:123456789012:instance/i-0def",
             "currentInstanceType": "t3.small", "finding": "Optimized", "recommendationOptions": []},
        ]})
        findings = rightsizing.overprovisioned_instances(_StubCtx(co))
    assert [(f.resource_id, f.monthly_cost) for f in findings] == [("i-0abc", 210.24)]


def test_compute_optimizer_not_enabled_raises_notice(aws):
    co = aws.client("compute-optimizer")
    with Stubber(co) as stub:
        stub.add_client_error("get_ec2_instance_recommendations", "OptInRequiredException")
        with pytest.raises(ServiceNotEnabled):
            rightsizing.overprovisioned_instances(_StubCtx(co))
