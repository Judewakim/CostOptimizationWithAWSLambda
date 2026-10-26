from waste_finder.pricing import ebs_volume_monthly, gp2_to_gp3_monthly_savings, monthly


def test_monthly_from_hourly():
    assert monthly(0.005) == 3.65
    assert monthly(0.045) == 32.85


def test_ebs_volume_costs():
    assert ebs_volume_monthly("gp2", 100) == 10.0
    assert ebs_volume_monthly("gp3", 100) == 8.0
    # io1 pays for provisioned IOPS too
    assert ebs_volume_monthly("io1", 100, iops=1000) == 12.5 + 65.0
    # gp3 only pays for performance above the free baseline
    assert ebs_volume_monthly("gp3", 100, iops=4000, throughput=225) == 8.0 + 5.0 + 4.0


def test_gp2_to_gp3_small_volume():
    assert gp2_to_gp3_monthly_savings(100) == 2.0


def test_gp2_to_gp3_accounts_for_iops_above_baseline():
    # 2000 GiB gp2 = 6000 IOPS; gp3 must buy 3000 extra IOPS to match.
    assert gp2_to_gp3_monthly_savings(2000) == round(200 - (160 + 15), 2)
