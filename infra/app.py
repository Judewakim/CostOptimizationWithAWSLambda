#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks import DemoStack, ScannerStack

app = cdk.App()
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                      region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"))

ScannerStack(app, "WasteFinderScanner", env=env,
             description="AWS Waste Finder: weekly read-only waste scan with email summary")

if app.node.try_get_context("enableDemo") in (True, "true"):
    DemoStack(app, "WasteFinderDemo", env=env,
              description="AWS Waste Finder: public scan-your-own-account demo site and API")

cdk.Tags.of(app).add("project", "aws-waste-finder")
app.synth()
