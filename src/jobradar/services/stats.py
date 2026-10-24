"""Pipeline numbers for `jobradar stats`: posts, jobs, reposts merged, your funnel."""

from __future__ import annotations

from jobradar.services.context import Ctx
from jobradar.stats import Report, build_report


def report(ctx: Ctx, days: int) -> Report:
    return build_report(ctx.repo, days, alert_above=ctx.good)
