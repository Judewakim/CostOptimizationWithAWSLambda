"""Static list prices used to estimate monthly cost.

These are us-east-1 on-demand list prices. Other regions are usually within
about 0-25% of these. Estimates are meant to rank waste and size the
opportunity, not to reproduce a bill to the cent.
"""

PRICES_REGION = "us-east-1"
PRICES_AS_OF = "2026-10"
HOURS_PER_MONTH = 730

# EBS storage, $ per GB-month
EBS_GB_MONTH = {
    "gp2": 0.10,
    "gp3": 0.08,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
    "standard": 0.05,
}
EBS_PIOPS_MONTH = 0.065          # io1/io2, $ per provisioned IOPS-month
GP3_FREE_IOPS = 3000
GP3_FREE_THROUGHPUT_MBPS = 125
GP3_IOPS_MONTH = 0.005           # $ per IOPS-month above 3,000
GP3_THROUGHPUT_MONTH = 0.04      # $ per MB/s-month above 125

EBS_SNAPSHOT_GB_MONTH = 0.05

PUBLIC_IPV4_HOUR = 0.005         # every public IPv4 address, since Feb 2024
NAT_GATEWAY_HOUR = 0.045
ALB_HOUR = 0.0225
NLB_HOUR = 0.0225
CLASSIC_ELB_HOUR = 0.025

S3_STANDARD_GB_MONTH = 0.023
CLOUDWATCH_LOGS_GB_MONTH = 0.03

GB = 1024 ** 3


def monthly(hourly: float) -> float:
    return round(hourly * HOURS_PER_MONTH, 2)


def ebs_volume_monthly(volume_type: str, size_gb: int, iops: int | None = None,
                       throughput: int | None = None) -> float:
    """Monthly cost of one EBS volume, including provisioned performance."""
    cost = size_gb * EBS_GB_MONTH.get(volume_type, EBS_GB_MONTH["gp2"])
    if volume_type in ("io1", "io2") and iops:
        cost += iops * EBS_PIOPS_MONTH
    if volume_type == "gp3":
        if iops and iops > GP3_FREE_IOPS:
            cost += (iops - GP3_FREE_IOPS) * GP3_IOPS_MONTH
        if throughput and throughput > GP3_FREE_THROUGHPUT_MBPS:
            cost += (throughput - GP3_FREE_THROUGHPUT_MBPS) * GP3_THROUGHPUT_MONTH
    return round(cost, 2)


def gp2_to_gp3_monthly_savings(size_gb: int) -> float:
    """Savings from converting gp2 to a gp3 volume with the same baseline IOPS.

    gp2 gives 3 IOPS per GB (min 100). gp3 includes 3,000 IOPS; anything above
    that has to be bought to keep the same performance.
    """
    gp2_iops = max(100, 3 * size_gb)
    extra_iops = max(0, gp2_iops - GP3_FREE_IOPS)
    gp2 = size_gb * EBS_GB_MONTH["gp2"]
    gp3 = size_gb * EBS_GB_MONTH["gp3"] + extra_iops * GP3_IOPS_MONTH
    return round(gp2 - gp3, 2)
