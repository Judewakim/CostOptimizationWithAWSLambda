"""Networking: NAT gateways and load balancers that carry no traffic."""
from __future__ import annotations

from datetime import timedelta

from botocore.exceptions import ClientError

from ..aws import CheckContext
from ..models import EXACT, Finding
from ..pricing import (ALB_HOUR, CLASSIC_ELB_HOUR, NAT_GATEWAY_HOUR, NLB_HOUR,
                       PUBLIC_IPV4_HOUR, monthly)
from .base import regional

LOOKBACK = timedelta(days=14)


def metric_sum(ctx: CheckContext, namespace: str, metric: str, dims: dict[str, str]) -> float:
    """Total of a CloudWatch metric over the lookback window (0 if no data)."""
    cw = ctx.client("cloudwatch")
    resp = cw.get_metric_statistics(
        Namespace=namespace, MetricName=metric,
        Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=ctx.now - LOOKBACK, EndTime=ctx.now,
        Period=86400, Statistics=["Sum"],
    )
    return sum(dp.get("Sum", 0.0) for dp in resp.get("Datapoints", []))


@regional("nat-idle")
def idle_nat_gateways(ctx: CheckContext) -> list[Finding]:
    out = []
    paginator = ctx.client("ec2").get_paginator("describe_nat_gateways")
    for page in paginator.paginate(Filters=[{"Name": "state", "Values": ["available"]}]):
        for nat in page["NatGateways"]:
            if ctx.now - nat["CreateTime"] < LOOKBACK:
                continue
            dims = {"NatGatewayId": nat["NatGatewayId"]}
            traffic = (metric_sum(ctx, "AWS/NATGateway", "BytesOutToDestination", dims)
                       + metric_sum(ctx, "AWS/NATGateway", "BytesInFromSource", dims))
            if traffic > 0:
                continue
            cost = monthly(NAT_GATEWAY_HOUR)
            if nat.get("ConnectivityType", "public") == "public":
                cost = round(cost + monthly(PUBLIC_IPV4_HOUR), 2)
            out.append(Finding(
                check="nat-idle",
                title="Idle NAT gateways",
                account_id=ctx.account_id, region=ctx.region,
                resource_id=nat["NatGatewayId"], resource_type="NAT gateway",
                monthly_cost=cost, confidence=EXACT,
                detail="No traffic in the last 14 days. NAT gateways bill by the hour "
                       "whether or not anything uses them.",
                action="Delete it and remove the route that points to it. "
                       "Release its Elastic IP afterwards.",
            ))
    return out


def _lb_dimension(arn: str) -> str:
    # arn:aws:elasticloadbalancing:region:acct:loadbalancer/app/name/id -> app/name/id
    return arn.split(":loadbalancer/", 1)[-1]


@regional("lb-idle")
def idle_load_balancers(ctx: CheckContext) -> list[Finding]:
    out = []
    elbv2 = ctx.client("elbv2")
    for page in elbv2.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            lb_type = lb.get("Type")
            if lb_type not in ("application", "network"):
                continue
            if ctx.now - lb["CreatedTime"] < LOOKBACK:
                continue
            arn = lb["LoadBalancerArn"]
            targets = 0
            try:
                groups = elbv2.describe_target_groups(LoadBalancerArn=arn).get("TargetGroups", [])
            except ClientError as e:
                if e.response["Error"]["Code"] != "TargetGroupNotFound":
                    raise
                groups = []
            for tg in groups:
                health = elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                targets += len(health.get("TargetHealthDescriptions", []))
            if targets == 0:
                reason = "has no registered targets"
            else:
                namespace, metric = (("AWS/ApplicationELB", "RequestCount") if lb_type == "application"
                                     else ("AWS/NetworkELB", "NewFlowCount"))
                if metric_sum(ctx, namespace, metric, {"LoadBalancer": _lb_dimension(arn)}) > 0:
                    continue
                reason = "received no traffic in the last 14 days"
            hourly = ALB_HOUR if lb_type == "application" else NLB_HOUR
            out.append(Finding(
                check="lb-idle",
                title="Idle load balancers",
                account_id=ctx.account_id, region=ctx.region,
                resource_id=lb["LoadBalancerName"],
                resource_type="ALB" if lb_type == "application" else "NLB",
                monthly_cost=monthly(hourly), confidence=EXACT,
                detail=f"Load balancer {reason}. The base hourly charge applies regardless.",
                action="Delete it if no DNS record or service points at it.",
            ))

    elb = ctx.client("elb")
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancerDescriptions"]:
            if lb.get("Instances") or ctx.now - lb["CreatedTime"] < LOOKBACK:
                continue
            out.append(Finding(
                check="lb-idle",
                title="Idle load balancers",
                account_id=ctx.account_id, region=ctx.region,
                resource_id=lb["LoadBalancerName"], resource_type="Classic ELB",
                monthly_cost=monthly(CLASSIC_ELB_HOUR), confidence=EXACT,
                detail="Classic load balancer with no registered instances.",
                action="Delete it if no DNS record points at it.",
            ))
    return out
