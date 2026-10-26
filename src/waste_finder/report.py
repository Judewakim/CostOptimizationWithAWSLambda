"""Report rendering: JSON for machines, a self-contained HTML page for people,
and a short plain-text summary for email."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape

from . import __version__
from .pricing import PRICES_AS_OF, PRICES_REGION


def build_report(scans: list[dict], generated_at: datetime) -> dict:
    total = round(sum(s["monthly_total"] for s in scans), 2)
    return {
        "tool": "aws-waste-finder",
        "version": __version__,
        "generated_at": generated_at.isoformat(),
        "pricing": {"region": PRICES_REGION, "as_of": PRICES_AS_OF},
        "monthly_total": total,
        "annual_total": round(total * 12, 2),
        "accounts": scans,
    }


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


def _money(v: float) -> str:
    return f"${v:,.2f}"


def render_html(report: dict) -> str:
    rows = []
    for acct in report["accounts"]:
        for f in acct["findings"]:
            rows.append(
                "<tr>"
                f"<td>{escape(f['account_id'])}</td>"
                f"<td>{escape(f['region'])}</td>"
                f"<td>{escape(f['title'])}</td>"
                f"<td><code>{escape(f['resource_id'])}</code></td>"
                f"<td class='num'>{_money(f['monthly_cost'])}</td>"
                f"<td>{escape(f['confidence'])}</td>"
                f"<td>{escape(f['detail'])}<br><em>{escape(f['action'])}</em></td>"
                "</tr>")
    issues = []
    for acct in report["accounts"]:
        issues += [f"{escape(acct['account_id'])}: {escape(n)}" for n in acct.get("notices", [])]
        issues += [f"{escape(acct['account_id'])} {escape(e['check'])} ({escape(e['region'])}): "
                   f"{escape(e['message'])}" for e in acct.get("errors", [])]
    issue_html = ("<h2>Scan notes</h2><ul>" + "".join(f"<li>{i}</li>" for i in issues) + "</ul>"
                  if issues else "")
    body = "".join(rows) or "<tr><td colspan='7'>No waste found.</td></tr>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWS Waste Report</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a;background:#fff}}
h1{{margin:0 0 .25rem}} .total{{font-size:2rem;font-weight:700;color:#0b6b3a}}
table{{border-collapse:collapse;width:100%;margin-top:1rem;font-size:.9rem}}
th,td{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}}
th{{background:#f4f4f4}} .num{{text-align:right;white-space:nowrap}}
em{{color:#555}} .meta{{color:#666;font-size:.85rem}}
</style></head><body>
<h1>AWS Waste Report</h1>
<p class="meta">Generated {escape(report['generated_at'])} by aws-waste-finder {escape(report['version'])}.
Prices: {escape(report['pricing']['region'])} list prices as of {escape(report['pricing']['as_of'])}; estimates, not a bill.</p>
<p class="total">{_money(report['monthly_total'])}/month &middot; {_money(report['annual_total'])}/year</p>
<table><thead><tr><th>Account</th><th>Region</th><th>Finding</th><th>Resource</th>
<th class="num">$/month</th><th>Confidence</th><th>Details &amp; fix</th></tr></thead>
<tbody>{body}</tbody></table>
{issue_html}
</body></html>"""


def email_summary(report: dict, console_link: str) -> str:
    """Totals only. Resource IDs stay in the report, which requires AWS sign-in to open."""
    lines = [
        "AWS Waste Finder - scheduled scan",
        "",
        f"Estimated waste: {_money(report['monthly_total'])}/month "
        f"({_money(report['annual_total'])}/year)",
        "",
    ]
    for acct in report["accounts"]:
        lines.append(f"Account {acct['account_id']}: {_money(acct['monthly_total'])}/month, "
                     f"{len(acct['findings'])} finding(s)")
        for check in list(acct["summary"].values())[:5]:
            lines.append(f"  - {check['title']}: {check['count']} ({_money(check['monthly_cost'])}/month)")
        if acct.get("errors"):
            lines.append(f"  ! {len(acct['errors'])} check(s) could not run; see report.")
    lines += ["", f"Full report (AWS sign-in required): {console_link}"]
    return "\n".join(lines)
