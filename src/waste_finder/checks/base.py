"""Check registry. A check takes a CheckContext and returns a list of Findings."""
from __future__ import annotations

from typing import Callable

from ..aws import CheckContext
from ..models import Finding

CheckFn = Callable[[CheckContext], list[Finding]]

# check id -> function. Regional checks run once per enabled region; global once per account.
REGIONAL: dict[str, CheckFn] = {}
GLOBAL: dict[str, CheckFn] = {}


def regional(check_id: str):
    def wrap(fn: CheckFn) -> CheckFn:
        REGIONAL[check_id] = fn
        return fn
    return wrap


def global_check(check_id: str):
    def wrap(fn: CheckFn) -> CheckFn:
        GLOBAL[check_id] = fn
        return fn
    return wrap


class ServiceNotEnabled(Exception):
    """The account hasn't turned on a service a check depends on. Reported once as a notice."""
