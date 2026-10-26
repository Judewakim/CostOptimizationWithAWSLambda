"""Run a scan from your laptop with whatever AWS credentials are active.

    python -m waste_finder.cli                      # current account, all enabled regions
    python -m waste_finder.cli --regions us-east-1  # one region
    python -m waste_finder.cli --html report.html   # also write an HTML report
"""
from __future__ import annotations

import argparse
import sys

import boto3

from .aws import current_account_id, utcnow
from .checks import ALL_CHECKS
from .report import build_report, render_html, to_json
from .scanner import scan_account


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Find idle and orphaned AWS resources.")
    p.add_argument("--profile")
    p.add_argument("--regions", nargs="*")
    p.add_argument("--checks", nargs="*", choices=ALL_CHECKS)
    p.add_argument("--json", help="write JSON report to this path")
    p.add_argument("--html", help="write HTML report to this path")
    args = p.parse_args(argv)

    session = boto3.session.Session(profile_name=args.profile)
    account = current_account_id(session)
    result = scan_account(session, account, regions=args.regions, checks=args.checks)
    report = build_report([result.to_dict()], utcnow())

    for f in sorted(result.findings, key=lambda f: -f.monthly_cost):
        print(f"${f.monthly_cost:>9,.2f}  {f.region:<15} {f.title:<34} {f.resource_id}")
    print(f"\nTotal: ${result.monthly_total:,.2f}/month (${result.monthly_total * 12:,.2f}/year)")
    for n in result.notices:
        print(f"note: {n}", file=sys.stderr)
    for e in result.errors:
        print(f"error: {e.check} {e.region}: {e.message}", file=sys.stderr)

    if args.json:
        with open(args.json, "w") as fh:
            fh.write(to_json(report))
    if args.html:
        with open(args.html, "w") as fh:
            fh.write(render_html(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
