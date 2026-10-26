"""Importing this package registers every check."""
from . import ebs, ec2, network, rightsizing, storage  # noqa: F401
from .base import GLOBAL, REGIONAL, ServiceNotEnabled

ALL_CHECKS = sorted([*REGIONAL, *GLOBAL])

__all__ = ["ALL_CHECKS", "GLOBAL", "REGIONAL", "ServiceNotEnabled"]
