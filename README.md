# 🚀 AWS Cost Optimization with Lambda

💰 **Save money on AWS with an automated cost optimization tool!** <br>This Lambda function identifies underutilized resources and provides actionable recommendations to reduce your cloud bill.

---

## 📌 Features

✅ **EC2 Analysis**: Detects EC2 instances with low CPU utilization. <br>
✅ **RDS Analysis**: Identifies RDS instances with low CPU usage. <br>
✅ **S3 Analysis**: Suggests storage class transitions for cost savings. <br>
✅ **Automated Alerts**: Sends cost-saving recommendations via AWS SNS. <br>

---

## 🔧 Prerequisites

Before you begin, ensure you have:

- ✅ An **AWS Account** with necessary permissions.
- ✅ **AWS CLI** installed and configured.
- ✅ A **Python 3.x** environment.
- ✅ The **Boto3** library installed (`pip install boto3`).
- ✅ An **SNS Topic ARN** for notifications.

---

## 🚀 Quick Start Guide

### 1️⃣ Clone the Repository

```bash
git clone https://github.com/yourusername/aws-cost-optimization.git
cd aws-cost-optimization
```

### 2️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

### 3️⃣ Configure AWS Lambda

- 🔹 **Create a new Lambda function** in the AWS Console.
- 🔹 Set the **runtime** to Python 3.x.
- 🔹 Upload `lambda_function.py` as the function code.
- 🔹 Set the environment variable `SNS_TOPIC_ARN` with your SNS Topic ARN.
- 🔹 Assign **IAM roles** with permissions for EC2, RDS, S3, and SNS.

### 4️⃣ Deploy and Test

- 🚀 Deploy the Lambda function.
- 🔄 Trigger manually or via **CloudWatch Scheduled Events**.
- 📩 Receive cost-saving recommendations via SNS notifications.

---

## 📜 License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

---

💡 **Pro Tip**: Set up a CloudWatch event to trigger this function regularly for **continuous cost savings!** 🔄💰

