"""CDK stacks for AWS Waste Finder.

ScannerStack  - scheduled weekly scan of your own account(s), report to S3, summary email.
DemoStack     - public "scan your own account" website + API (used by the portfolio demo).
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROLE_NAME = "WasteFinderReadOnly"

# Exactly what cloudformation/readonly-role.yaml grants. Metadata and metrics only.
READ_ONLY_ACTIONS = [
    "ec2:DescribeRegions", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages",
    "ec2:DescribeAddresses", "ec2:DescribeInstances", "ec2:DescribeNatGateways",
    "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups",
    "elasticloadbalancing:DescribeTargetHealth",
    "cloudwatch:GetMetricStatistics",
    "s3:ListAllMyBuckets", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads",
    "s3:ListMultipartUploadParts",
    "logs:DescribeLogGroups",
    "compute-optimizer:GetEC2InstanceRecommendations",
]


def _code() -> _lambda.Code:
    return _lambda.Code.from_asset(str(ROOT / "src"),
                                   exclude=["**/__pycache__", "**/*.pyc", "*.egg-info"])


def _secure_bucket(scope: Construct, cid: str, expire_days: int, retain: bool,
                   **kwargs) -> s3.Bucket:
    return s3.Bucket(
        scope, cid,
        encryption=s3.BucketEncryption.S3_MANAGED,
        block_public_access=kwargs.pop("block_public_access", s3.BlockPublicAccess.BLOCK_ALL),
        enforce_ssl=True,
        object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(expire_days),
                                          abort_incomplete_multipart_upload_after=Duration.days(1))],
        removal_policy=RemovalPolicy.RETAIN if retain else RemovalPolicy.DESTROY,
        auto_delete_objects=not retain,
        **kwargs,
    )


def _function(scope: Construct, cid: str, handler: str, timeout: Duration, memory: int,
              env: dict, role_name: str | None = None) -> _lambda.Function:
    log_group = logs.LogGroup(scope, f"{cid}Logs", retention=logs.RetentionDays.ONE_MONTH,
                              removal_policy=RemovalPolicy.DESTROY)
    role = iam.Role(scope, f"{cid}Role", role_name=role_name,
                    assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    role.add_to_policy(iam.PolicyStatement(
        actions=["logs:CreateLogStream", "logs:PutLogEvents"],
        resources=[log_group.log_group_arn, f"{log_group.log_group_arn}:*"]))
    return _lambda.Function(
        scope, cid,
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler=handler,
        code=_code(),
        timeout=timeout,
        memory_size=memory,
        environment=env,
        role=role,
        log_group=log_group,
    )


def _allow_assume_scan_role(fn: _lambda.Function) -> None:
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sts:AssumeRole"], resources=[f"arn:aws:iam::*:role/{SCAN_ROLE_NAME}"]))


class ScannerStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        ctx = self.node.try_get_context
        notify_email = ctx("notifyEmail")
        target_accounts = ctx("targetAccountIds") or ""
        scan_regions = ctx("scanRegions") or ""
        external_id_param = ctx("externalIdParameter")

        bucket = _secure_bucket(self, "Reports", expire_days=180, retain=True)
        topic = sns.Topic(self, "Summary", display_name="AWS Waste Finder",
                          master_key=kms.Alias.from_alias_name(self, "SnsKey", "alias/aws/sns"))
        if notify_email:
            topic.add_subscription(subs.EmailSubscription(notify_email))

        env = {"REPORT_BUCKET": bucket.bucket_name, "SNS_TOPIC_ARN": topic.topic_arn,
               "ROLE_NAME": SCAN_ROLE_NAME, "TARGET_ACCOUNT_IDS": target_accounts,
               "SCAN_REGIONS": scan_regions}
        if external_id_param:
            env["EXTERNAL_ID_PARAMETER"] = external_id_param

        fn = _function(self, "ScheduledScan", "waste_finder.handlers.scheduled.handler",
                       Duration.minutes(10), 1024, env)
        fn.add_to_role_policy(iam.PolicyStatement(actions=READ_ONLY_ACTIONS + ["sts:GetCallerIdentity"],
                                                  resources=["*"]))
        _allow_assume_scan_role(fn)
        bucket.grant_put(fn)
        topic.grant_publish(fn)
        if external_id_param:
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self.format_arn(service="ssm", resource="parameter",
                                           resource_name=external_id_param.lstrip("/"))]))

        events.Rule(self, "Weekly",
                    schedule=events.Schedule.cron(minute="0", hour="12", week_day="MON"),
                    targets=[targets.LambdaFunction(fn, retry_attempts=1)])

        CfnOutput(self, "ReportBucket", value=bucket.bucket_name)
        CfnOutput(self, "ScannerRoleArn", value=fn.role.role_arn,
                  description="Use as ScannerRoleArn when deploying readonly-role.yaml in other accounts")


class DemoStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        extra_origins = [o for o in (self.node.try_get_context("allowedOrigins") or "").split(",") if o]

        table = dynamodb.Table(
            self, "Sessions",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiresAt",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        results = _secure_bucket(self, "Results", expire_days=1, retain=False)

        # The role template must be publicly readable for CloudFormation quick-create.
        # This bucket holds that one file and nothing else.
        templates = _secure_bucket(
            self, "Templates", expire_days=3650, retain=False,
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=True, ignore_public_acls=True,
                block_public_policy=False, restrict_public_buckets=False))
        templates.add_to_resource_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], principals=[iam.AnyPrincipal()],
            resources=[templates.arn_for_objects("templates/readonly-role.yaml")]))
        s3deploy.BucketDeployment(
            self, "DeployTemplate", destination_bucket=templates,
            sources=[s3deploy.Source.asset(str(ROOT / "cloudformation"))],
            destination_key_prefix="templates/", prune=True)
        template_url = (f"https://{templates.bucket_name}.s3.{self.region}.amazonaws.com"
                        "/templates/readonly-role.yaml")

        common = {"TABLE_NAME": table.table_name, "RESULTS_BUCKET": results.bucket_name,
                  "ROLE_NAME": SCAN_ROLE_NAME}
        worker = _function(self, "Worker", "waste_finder.handlers.worker.handler",
                           Duration.minutes(10), 1024, common,
                           role_name=f"WasteFinderDemoWorker-{self.region}")
        _allow_assume_scan_role(worker)
        table.grant_read_write_data(worker)
        results.grant_put(worker)

        api_fn = _function(self, "Api", "waste_finder.handlers.api.handler",
                           Duration.seconds(10), 256,
                           {**common, "WORKER_FUNCTION": worker.function_name,
                            "TEMPLATE_URL": template_url,
                            "SCANNER_ROLE_ARN": worker.role.role_arn})
        table.grant_read_write_data(api_fn)
        results.grant_read(api_fn)
        worker.grant_invoke(api_fn)

        # Static website: private bucket behind CloudFront with strict security headers.
        site_bucket = _secure_bucket(self, "Site", expire_days=3650, retain=False)
        headers = cloudfront.ResponseHeadersPolicy(
            self, "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' data:; "
                        f"connect-src 'self' https://*.execute-api.{self.region}.amazonaws.com; "
                        "frame-ancestors 'self' https://wakimworks.com https://www.wakimworks.com; "
                        "base-uri 'none'; form-action 'none'"),
                    override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True),
            ))
        site = cloudfront.Distribution(
            self, "Website",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
            ),
        )
        site_origin = f"https://{site.distribution_domain_name}"

        http_api = apigw.HttpApi(
            self, "HttpApi",
            create_default_stage=False,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[site_origin, *extra_origins],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST],
                allow_headers=["content-type", "x-session-id"],
                max_age=Duration.hours(1)),
        )
        apigw.HttpStage(self, "Stage", http_api=http_api, stage_name="$default", auto_deploy=True,
                        throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10))
        integration = integrations.HttpLambdaIntegration("ApiIntegration", api_fn)
        for path, method in (("/sessions", apigw.HttpMethod.POST),
                             ("/scans", apigw.HttpMethod.POST),
                             ("/scans/{scanId}", apigw.HttpMethod.GET)):
            http_api.add_routes(path=path, methods=[method], integration=integration)

        s3deploy.BucketDeployment(
            self, "DeploySite", destination_bucket=site_bucket,
            sources=[s3deploy.Source.asset(str(ROOT / "web")),
                     s3deploy.Source.json_data("config.json", {"apiUrl": http_api.api_endpoint})],
            distribution=site, distribution_paths=["/*"], prune=True)

        CfnOutput(self, "SiteUrl", value=site_origin)
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "TemplateUrl", value=template_url)
        CfnOutput(self, "WorkerRoleArn", value=worker.role.role_arn)
