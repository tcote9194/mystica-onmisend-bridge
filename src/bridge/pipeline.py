"""The run orchestration — wires sources -> desired -> plan -> apply -> report.

Kept separate from ``cli.py`` (arg parsing) so the pipeline is importable and
testable on its own. Each ``run_*`` returns the :class:`RunReport` and the built
:class:`ChangePlan` so a caller (CLI or test) can inspect both.
"""

from __future__ import annotations

import logging

from bridge import config, desired as desired_mod, report as report_mod
from bridge.apply import apply_plan
from bridge.identity import IdentityBridge
from bridge.models import ChangePlan, RunReport
from bridge.omnisend import OmniSendClient
from bridge.plan import build_plan
from bridge.posthog import PostHogClient, PostHogError
from bridge.render_db import RenderDB

log = logging.getLogger("bridge.pipeline")


def _mode() -> str:
    return "live" if config.external_writes_enabled() else "dry_run"


def _interaction_aggregates(render, posthog, *, since_days, report):
    """Fetch interaction aggregates from the configured source (Render by default,
    PostHog optional). Both degrade to ``[]`` on failure with a DEGRADED note — an
    empty pull only means "no interaction tags this run", never a data wipe (the
    bridge never removes tags)."""
    src = config.interaction_source()
    report.notes.append(f"interaction source: {src}")
    if src == "render":
        try:
            return render.fetch_interaction_aggregates()
        except Exception as exc:  # DB blip mid-run — degrade, don't crash the sync
            report.notes.append(f"DEGRADED: render interaction: {exc}")
            log.error("render interaction failed: %s", exc)
            return []
    try:
        return posthog.aggregate(since_days=since_days)
    except PostHogError as exc:
        report.notes.append(f"DEGRADED: {exc}")
        log.error("posthog interaction failed: %s", exc)
        return []


def _finalize(
    plan: ChangePlan,
    report: RunReport,
    *,
    live: bool,
    force: bool,
    write_findings: bool,
) -> tuple[RunReport, ChangePlan]:
    """Apply (if live) + print + persist + alert. Shared tail for every phase."""
    report.plan_touched = plan.touched
    report.plan_creates = len(plan.creates)
    report.diff_fraction = plan.diff_fraction
    if live and config.external_writes_enabled():
        applied, created, errors = apply_plan(plan, force=force)
        report.applied = applied
        report.created = created
        report.errors = errors
    text = report_mod.render_report(plan, report)
    print(text)
    anomaly = plan.diff_fraction > config.max_diff_fraction()
    if write_findings:
        report_mod.write_findings(report, text)
    report_mod.post_summary(report, anomaly=anomaly)
    return report, plan


def run_membership(
    *,
    live: bool = False,
    force: bool = False,
    max_contacts: int | None = None,
    write_findings: bool = True,
    create_missing: bool = False,
    render: RenderDB | None = None,
    omnisend: OmniSendClient | None = None,
) -> tuple[RunReport, ChangePlan]:
    """Phase 2 — Render -> OmniSend membership sync (the quick win, no PostHog join)."""
    render = render or RenderDB()
    omnisend = omnisend or OmniSendClient()
    report = RunReport(phase="membership", mode=_mode())

    roster = render.fetch_roster()
    report.render_users = len(roster)
    report.render_with_email = sum(1 for u in roster if u.has_email)

    desired = desired_mod.membership_desired(roster)
    current = omnisend.load_current(max_contacts=max_contacts)
    report.omnisend_contacts = len(current)

    plan = build_plan(desired, current, create_missing=create_missing)
    return _finalize(plan, report, live=live, force=force, write_findings=write_findings)


def run_interaction(
    *,
    live: bool = False,
    force: bool = False,
    since_days: int | None = None,
    max_contacts: int | None = None,
    write_findings: bool = True,
    create_missing: bool = False,
    render: RenderDB | None = None,
    posthog: PostHogClient | None = None,
    omnisend: OmniSendClient | None = None,
) -> tuple[RunReport, ChangePlan]:
    """Phase 3 — PostHog (joined via Render) -> OmniSend interaction sync."""
    render = render or RenderDB()
    posthog = posthog or PostHogClient()
    omnisend = omnisend or OmniSendClient()
    report = RunReport(phase="interaction", mode=_mode())

    roster = render.fetch_roster()
    report.render_users = len(roster)
    report.render_with_email = sum(1 for u in roster if u.has_email)
    identity = IdentityBridge(roster)

    aggregates = _interaction_aggregates(render, posthog, since_days=since_days, report=report)
    report.posthog_rows = len(aggregates)

    desired, resolved, unresolved = desired_mod.interaction_desired(aggregates, identity)
    report.resolved_ids = resolved
    report.unresolved_ids = unresolved

    current = omnisend.load_current(max_contacts=max_contacts)
    report.omnisend_contacts = len(current)

    plan = build_plan(desired, current, create_missing=create_missing)
    return _finalize(plan, report, live=live, force=force, write_findings=write_findings)


def run_all(
    *,
    live: bool = False,
    force: bool = False,
    since_days: int | None = None,
    max_contacts: int | None = None,
    write_findings: bool = True,
    create_missing: bool = False,
    render: RenderDB | None = None,
    posthog: PostHogClient | None = None,
    omnisend: OmniSendClient | None = None,
) -> tuple[RunReport, ChangePlan]:
    """Full sync — membership + interaction combined into one plan/apply.

    Sharing one plan means a contact that gains both an install fact and a chat fact
    is written once, and the guardrail sees the true combined blast radius.
    """
    render = render or RenderDB()
    posthog = posthog or PostHogClient()
    omnisend = omnisend or OmniSendClient()
    report = RunReport(phase="full", mode=_mode())

    roster = render.fetch_roster()
    report.render_users = len(roster)
    report.render_with_email = sum(1 for u in roster if u.has_email)
    identity = IdentityBridge(roster)

    membership = desired_mod.membership_desired(roster)
    aggregates = _interaction_aggregates(render, posthog, since_days=since_days, report=report)
    report.posthog_rows = len(aggregates)

    interaction, resolved, unresolved = desired_mod.interaction_desired(aggregates, identity)
    report.resolved_ids = resolved
    report.unresolved_ids = unresolved

    # Soulmate reading fulfillment stages (email-keyed, NOT recency-gated).
    try:
        reading_map = render.fetch_reading_stages()
    except Exception as exc:
        report.notes.append(f"DEGRADED: reading stages: {exc}")
        log.error("reading stages failed: %s", exc)
        reading_map = {}
    reading = desired_mod.reading_stages_desired(reading_map)
    report.notes.append(f"reading stages: {len(reading)} contacts")

    desired = desired_mod.combine(membership, interaction, reading)
    current = omnisend.load_current(max_contacts=max_contacts)
    report.omnisend_contacts = len(current)

    plan = build_plan(desired, current, create_missing=create_missing)
    return _finalize(plan, report, live=live, force=force, write_findings=write_findings)
