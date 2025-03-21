import boto3
import json
import datetime
import botocore
import os

# Initialize AWS clients
ec2_client = boto3.client("ec2")
cloudwatch_client = boto3.client("cloudwatch")
rds_client = boto3.client("rds")
s3_client = boto3.client("s3")
sns_client = boto3.client("sns")

#environment variables in lambda
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")

if not SNS_TOPIC_ARN:
    raise ValueError("SNS_TOPIC_ARN environment variable is not set!")

#function for collecting EC2 data
def get_low_utilization_ec2(threshold=5, period=3600, days=7):
    """Find EC2 instances with CPU usage below a threshold over a period of time."""
    instances = ec2_client.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
    underutilized = []

    for reservation in instances["Reservations"]:
        for instance in reservation["Instances"]:
            instance_id = instance["InstanceId"]
            avg_cpu = cloudwatch_client.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=datetime.datetime.utcnow() - datetime.timedelta(days=days),
                EndTime=datetime.datetime.utcnow(),
                Period=period,
                Statistics=["Average"]
            )

            if avg_cpu["Datapoints"] and avg_cpu["Datapoints"][0]["Average"] < threshold:
                underutilized.append(instance_id)

    return underutilized

#function for collecting RDS data
def get_low_utilization_rds(threshold=10, period=3600, days=7):
    """Find RDS instances with low CPU usage."""
    databases = rds_client.describe_db_instances()
    underutilized = []

    for db in databases["DBInstances"]:
        db_id = db["DBInstanceIdentifier"]
        avg_cpu = cloudwatch_client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
            StartTime=datetime.datetime.utcnow() - datetime.timedelta(days=days),
            EndTime=datetime.datetime.utcnow(),
            Period=period,
            Statistics=["Average"]
        )

        if avg_cpu["Datapoints"] and avg_cpu["Datapoints"][0]["Average"] < threshold:
            underutilized.append(db_id)

    return underutilized

#function for collecting s3 data
def get_s3_storage_savings():
    """Find S3 buckets that could benefit from lifecycle policies, if applicable."""
    buckets = s3_client.list_buckets()
    savings = {}

    for bucket in buckets["Buckets"]:
        bucket_name = bucket["Name"]
        try:
            response = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
            if response.get("Rules"):
                savings[bucket_name] = "Consider moving infrequent objects to S3 Intelligent-Tiering."
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                # Skip this bucket as it has no lifecycle policy
                continue  
            else:
                raise  # Raise other unexpected errors

    return savings

#function for sending SNS notification
def send_notification(message):
    """Send cost optimization recommendations via SNS."""
    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=json.dumps(message),
        Subject="AWS Cost Optimization Recommendations"
    )

#main function
def lambda_handler(event, context):
    """Main Lambda function to check for cost-saving opportunities."""
    recommendations = {
        "Underutilized EC2 Instances": get_low_utilization_ec2(),
        "Underutilized RDS Databases": get_low_utilization_rds(),
        "S3 Storage Optimization": get_s3_storage_savings(),
    }

    send_notification(recommendations)
    return recommendations
