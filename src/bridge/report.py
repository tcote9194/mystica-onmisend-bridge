"""Run reporting — the human-readable diff + the one-line summary + Slack chip.

Every run prints a diff report (what WOULD change in dry-run; what DID in live) and
emits a compact ``RunReport``. The report is also appended to ``FINDINGS.md`` so the
paper trail of "the fix worked" (before/after counts, resolution rates) lives in the
repo, per EXECUTION_PLAN §5.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Optional

from bridge import config, slack
from bridge.models import ChangePlan, RunReport

log = logging.getLogger("bridge.report")


def _mask_email(email: str) -> str:
    """Domain-preserving mask so run reports (console + FINDINGS.md) never carry PII."""
    local, sep, domain = str(email).partition("@")
    if not sep:
        return "***"
    return f"{local[:2]}***@{domain}"


def summarize_tags(plan: ChangePlan) -> Counter:
    """How many contacts each tag would be added to (the headline 'N tagged app:
    install vs the ~600 baseline' number)."""
    c: Counter = Counter()
    for change in plan.changes:
        for tag in change.add_tags:
            c[tag] += 1
    return c


def summarize_props(plan: ChangePlan) -> Counter:
    c: Counter = Counter()
    for change in plan.changes:
        for prop in change.set_props:
            c[prop] += 1
    return c


def render_report(plan: ChangePlan, report: RunReport, *, sample: int = 10) -> str:
    """A multi-line, reviewable diff report."""
    lines: list[str] = []
    verb = "WOULD change" if report.mode == "dry_run" else "changed"
    lines.append(f"=== Tagging Bridge · {report.phase} · mode={report.mode} ===")
    lines.append(
        f"Render users: {report.render_users} "
        f"({report.render_with_email} with email)"
    )
    if report.posthog_rows:
        lines.append(
            f"PostHog rows: {report.posthog_rows} "
            f"(resolved {report.resolved_ids}, unresolved {report.unresolved_ids})"
        )
    lines.append(f"OmniSend contacts scanned: {report.omnisend_contacts}")
    lines.append(
        f"Desired contacts: {plan.desired_count} "
        f"(matched {plan.matched_count}, unresolved {plan.unresolved_emails})"
    )
    lines.append(
        f"Plan: {verb} {plan.touched} contacts "
        f"({plan.diff_fraction:.1%} of audience)"
    )
    tags = summarize_tags(plan)
    if tags:
        lines.append("  tags to add:")
        for tag, n in tags.most_common():
            lines.append(f"    {tag!r}: {n}")
    props = summarize_props(plan)
    if props:
        lines.append("  properties to set:")
        for prop, n in props.most_common():
            lines.append(f"    {prop}: {n}")
    if plan.changes and sample:
        shown = min(sample, len(plan.changes))
        lines.append(f"  sample ({shown} of {len(plan.changes)}):")
        for change in plan.changes[:sample]:
            lines.append(
                f"    {_mask_email(change.email)}: +tags={list(change.add_tags)} "
                f"props={list(change.set_props)}"
            )
    if report.mode != "dry_run":
        lines.append(f"Applied: {report.applied}  Errors: {report.errors}")
    if report.notes:
        lines.append("Notes: " + "; ".join(report.notes))
    return "\n".join(lines)


def write_findings(report: RunReport, text: str, *, root: Optional[Path] = None) -> Path:
    """Persist the run report (JSON + text) to ``data/runs/`` — which is gitignored,
    so run artifacts (which may reference contacts) never reach the repo. Returns the
    text path. FINDINGS.md is a hand-written, PII-free investigation doc; runs are NOT
    appended to it."""
    root = root or config.REPO_ROOT
    runs_dir = root / "data" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report.phase}-{report.mode}"
    (runs_dir / f"{stem}.json").write_text(
        json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    txt_path = runs_dir / f"{stem}.txt"
    txt_path.write_text(text, encoding="utf-8")
    return txt_path


def post_summary(report: RunReport, *, anomaly: bool = False) -> None:
    """Post a Slack chip on failure OR anomaly (never silently degrade)."""
    if report.errors or anomaly:
        icon = "🔴" if report.errors else "🟡"
        slack.alert(
            f"{icon} Tagging bridge {report.phase} ({report.mode}): "
            f"touched {report.plan_touched}, applied {report.applied}, "
            f"errors {report.errors}, unresolved_ids {report.unresolved_ids}"
        )
