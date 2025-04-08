import boto3
import json
import botocore
import os
import uuid
from fpdf import FPDF
from datetime import datetime

# Initialize AWS clients
sts_client = boto3.client("sts")
sns_client = boto3.client("sns")
s3_client = boto3.client("s3")

# Environment variables in Lambda
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
ASSUMABLE_ROLE_NAME = os.environ.get("ASSUMABLE_ROLE_NAME")  # IAM role to assume in each account
BUCKET_NAME = os.environ["PDF_REPORT_BUCKET"]  # set in Lambda env vars

if not SNS_TOPIC_ARN or not ASSUMABLE_ROLE_NAME:
    raise ValueError("SNS_TOPIC_ARN and ASSUMABLE_ROLE_NAME environment variables must be set!")

date_str = datetime.utcnow().strftime("%Y-%m-%d")
pdf_path = "/tmp/cost_optimization_report.pdf"

# Function to assume role in target account
def assume_role(account_id, role_name="CrossAccountAccessRole"):
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    """Assume a role in a different AWS account and return temporary credentials."""
    response = sts_client.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{ASSUMABLE_ROLE_NAME}",
        RoleSessionName="CostOptimizationSession"
    )
    return response["Credentials"]

# Function to get AWS clients for a specific account
def get_clients(credentials):
    """Return AWS service clients using assumed role credentials."""
    session = boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"]
    )
    return {
        "ec2": session.client("ec2"),
        "cloudwatch": session.client("cloudwatch"),
        "rds": session.client("rds"),
        "s3": session.client("s3")
    }

# Function to generate a PDF report
def generate_pdf_report(recommendations):
    """Create a PDF report summarizing the cost optimization findings."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(200, 10, "AWS Cost Optimization Report", ln=True, align='C')
    pdf.ln(10)
    
    for account_id, details in recommendations.items():
        pdf.set_font("Arial", "B", 12)
        pdf.cell(200, 10, f"Account: {account_id}", ln=True, align='L')
        pdf.ln(5)
        
        for category, items in details.items():
            pdf.set_font("Arial", "B", 11)
            pdf.cell(200, 8, f"{category}", ln=True, align='L')
            pdf.set_font("Arial", "", 10)
            
            if items:
                for item in items:
                    pdf.cell(200, 6, f"- {item}", ln=True, align='L')
            else:
                pdf.cell(200, 6, "- No optimizations found", ln=True, align='L')
            pdf.ln(5)
    
    pdf_output_path = f"/tmp/cost_optimization_report_{uuid.uuid4().hex}.pdf"
    pdf.output(pdf_output_path)
    return pdf_output_path

# Function to check if a timestamp falls on a weekend
def is_weekend(timestamp):
    return timestamp.weekday() >= 5  # Saturday (5) or Sunday (6)

# Function to retrieve P95 percentile metric
def get_percentile_metric(client, instance_id, namespace, metric_name, dimension_name, percentile="p95.0", period=86400, days=30):
    """Retrieve the specified percentile for a given metric."""
    response = client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=[{"Name": dimension_name, "Value": instance_id}],
        StartTime=datetime.datetime.utcnow() - datetime.timedelta(days=days),
        EndTime=datetime.datetime.utcnow(),
        Period=period,
        ExtendedStatistics=[percentile]
    )
    return response.get('Datapoints', [])

# Function to find low-utilization EC2 instances
def get_low_utilization_ec2(clients, threshold=5, period=86400, days=30):
    """Find EC2 instances with low CPU usage using P95 percentiles."""
    instances = clients["ec2"].describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
    underutilized = []

    for reservation in instances["Reservations"]:
        for instance in reservation["Instances"]:
            instance_id = instance["InstanceId"]
            datapoints = get_percentile_metric(
                clients["cloudwatch"],
                instance_id,
                "AWS/EC2",
                "CPUUtilization",
                "InstanceId",
                "p95",
                period,
                days
            )

            if not datapoints:
                continue  # Skip instance if no data

            weekday_usage = [data.get('ExtendedStatistics', {}).get('p95.0') or data.get('p95.0') for data in datapoints if not is_weekend(data['Timestamp'])]
            weekend_usage = [data.get('ExtendedStatistics', {}).get('p95.0') or data.get('p95.0') for data in datapoints if is_weekend(data['Timestamp'])]

            # Filter out None values
            weekday_usage = [v for v in weekday_usage if v is not None]
            weekend_usage = [v for v in weekend_usage if v is not None]

            if not weekday_usage and not weekend_usage:
                continue

            weekday_avg = sum(weekday_usage) / len(weekday_usage) if weekday_usage else 0
            weekend_avg = sum(weekend_usage) / len(weekend_usage) if weekend_usage else 0

            if weekday_avg < threshold and weekend_avg < threshold:
                underutilized.append(instance_id)

    return underutilized


# Function to identify underutilized RDS instances
def get_low_utilization_rds(clients, threshold=10, period=86400, days=30):
    """Find RDS instances with low CPU usage using P95 percentiles."""
    databases = clients["rds"].describe_db_instances()
    underutilized = []

    for db in databases["DBInstances"]:
        db_id = db["DBInstanceIdentifier"]
        datapoints = get_percentile_metric(
            clients["cloudwatch"],
            db_id,
            "AWS/RDS",
            "CPUUtilization",
            "DBInstanceIdentifier",
            "p95",
            period,
            days
        )

        if not datapoints:
            continue

        weekday_usage = [data.get('ExtendedStatistics', {}).get('p95.0') or data.get('p95.0') for data in datapoints if not is_weekend(data['Timestamp'])]
        weekend_usage = [data.get('ExtendedStatistics', {}).get('p95.0') or data.get('p95.0') for data in datapoints if is_weekend(data['Timestamp'])]

        weekday_usage = [v for v in weekday_usage if v is not None]
        weekend_usage = [v for v in weekend_usage if v is not None]

        if not weekday_usage and not weekend_usage:
            continue

        weekday_avg = sum(weekday_usage) / len(weekday_usage) if weekday_usage else 0
        weekend_avg = sum(weekend_usage) / len(weekend_usage) if weekend_usage else 0

        if weekday_avg < threshold and weekend_avg < threshold:
            underutilized.append(db_id)

    return underutilized


# Function to identify S3 storage savings
def get_s3_storage_savings(clients):
    """Find S3 buckets that could benefit from lifecycle policies."""
    buckets = clients["s3"].list_buckets()
    savings = {}

    for bucket in buckets["Buckets"]:
        bucket_name = bucket["Name"]
        try:
            response = clients["s3"].get_bucket_lifecycle_configuration(Bucket=bucket_name)
            if response.get("Rules"):
                savings[bucket_name] = "Consider moving infrequent objects to S3 Intelligent-Tiering."
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                continue  
            else:
                print(f"Error processing bucket {bucket_name}: {e}")
                continue

    return savings

# Function to send SNS notification
def send_notification(message, presigned_url):
    """Send cost optimization recommendations via SNS with a formatted message."""

    if not message:
        formatted_message = "No cost optimization recommendations found."
    else:
        formatted_message = "\n".join(
            [f"{category}:\n  - " + "\n  - ".join(items) if items else f"{category}:\n  - No optimizations found"
             for category, items in message.items()]
        )

    # Truncate if too long for SNS limits
    if len(formatted_message) > 250000:
        formatted_message = formatted_message[:250000] + "\n\n[Message Truncated]"

    # Construct the final message body
    full_message = (
        "AWS Cost Optimization Recommendations:\n\n"
        f"{formatted_message}\n\n"
        f"📎 Download the full report (valid for 1 hour):\n{presigned_url}"
    )

    # Send SNS notification
    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=full_message,
        Subject="AWS Cost Optimization Recommendations"
    )



# Main Lambda function
def lambda_handler(event, context):
    """Main Lambda function to check for cost-saving opportunities across multiple accounts."""
    accounts = event.get("account_ids", [])

    # Ensure accounts is always a list
    if isinstance(accounts, str):  # If it's a single account ID as a string
        accounts = [accounts]

    # If no accounts are provided, run in the current AWS account
    if not accounts:
        accounts = [boto3.client("sts").get_caller_identity()["Account"]]

    all_recommendations = {}

    for account_id in accounts:
        credentials = assume_role(account_id)
        clients = get_clients(credentials)
        
        recommendations = {
            "Underutilized EC2 Instances": get_low_utilization_ec2(clients),
            "Underutilized RDS Databases": get_low_utilization_rds(clients),
            "S3 Storage Optimization": get_s3_storage_savings(clients),
        }
        all_recommendations[account_id] = recommendations

    # Generate PDF locally
    pdf_path = generate_pdf_report(all_recommendations)

    s3_key = f"reports/{account_id}/{os.path.basename(pdf_path)}_{date_str}"
    s3_client.upload_file(pdf_path, BUCKET_NAME, s3_key)
    print(f"✅ PDF report uploaded to {BUCKET_NAME}/{s3_key}")

    # Generate presigned URL
    presigned_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": s3_key},
        ExpiresIn=3600
    )
    print(f"🔗 Presigned URL: {presigned_url}")

    # Now send the email with link
    send_notification(all_recommendations, presigned_url)
    pdf_path = generate_pdf_report(all_recommendations)

    return all_recommendations