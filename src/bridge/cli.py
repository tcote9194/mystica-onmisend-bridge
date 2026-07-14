"""Command-line entry point.

Subcommands:
  validate     — check config for the requested run; print effective mode.
  probe        — Phase 0.5 join validation: sample N PostHog distinct_ids, resolve
                 to email via Render, confirm the email exists in OmniSend. Read-only.
  baseline     — Phase 0.6 snapshot: current OmniSend counts (app:* tags,
                 posthog_distinct_id set, legacy tags). Read-only.
  membership   — Phase 2 Render -> OmniSend membership sync.
  interaction  — Phase 3 PostHog -> OmniSend interaction sync.
  run          — full sync (membership + interaction).

SAFETY: every sync is dry-run by default. A live write needs BOTH ``DRY_RUN=false``
in the environment AND the explicit ``--live`` flag here — two independent gates.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter

from bridge import config, pipeline
from bridge.identity import IdentityBridge, normalize_email
from bridge.omnisend import OmniSendClient
from bridge.posthog import PostHogClient
from bridge.render_db import RenderDB


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _effective_mode(live: bool) -> str:
    """The mode the run will ACTUALLY use — live only if both gates agree."""
    if live and config.external_writes_enabled():
        return "live"
    return "dry_run"


# --------------------------------------------------------------------- commands
def cmd_validate(args) -> int:
    require_posthog = (
        args.command in ("interaction", "run")
        and config.interaction_source() == "posthog"
    )
    try:
        config.validate(require_render=True, require_posthog=require_posthog)
    except config.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    print(f"Config OK. DRY_RUN={config.is_dry_run()} "
          f"external_writes_enabled={config.external_writes_enabled()}")
    return 0


def cmd_probe(args) -> int:
    """Prove the join round-trips: distinct_id -> Render email -> OmniSend contact."""
    render = RenderDB()
    posthog = PostHogClient()
    omnisend = OmniSendClient()

    roster = render.fetch_roster()
    identity = IdentityBridge(roster)
    print(f"Render roster: {len(roster)} users, {len(identity)} with (id, email).")

    if not posthog.configured:
        print("PostHog not configured — set POSTHOG_API_KEY to run the full join probe.")
        return 0

    aggregates = posthog.aggregate(since_days=args.since_days)
    sample = aggregates[: args.sample]
    print(f"PostHog: {len(aggregates)} active distinct_ids; probing {len(sample)}.")

    resolved = 0
    in_omnisend = 0
    for agg in sample:
        email = identity.email_for(agg.distinct_id)
        status = "NO EMAIL IN RENDER"
        if email:
            resolved += 1
            contact = omnisend.get_contact_by_email(email)
            if contact:
                in_omnisend += 1
                status = f"{email} -> OmniSend OK"
            else:
                status = f"{email} -> NOT in OmniSend"
        print(f"  {agg.distinct_id}: {status}")
    print(
        f"Join: {resolved}/{len(sample)} resolved to email, "
        f"{in_omnisend}/{len(sample)} reached an OmniSend contact."
    )
    return 0


def cmd_baseline(args) -> int:
    """Snapshot the current OmniSend state the later phases prove themselves against."""
    omnisend = OmniSendClient()
    app_tag_contacts = 0
    distinct_id_set = 0
    tag_counts: Counter = Counter()
    total = 0
    for c in omnisend.iter_contacts(max_contacts=args.max_contacts):
        total += 1
        tags = [str(t) for t in (c.get("tags") or [])]
        cp = c.get("customProperties") or {}
        has_app_tag = False
        for t in tags:
            tag_counts[t] += 1
            if t.startswith("app:") or t.startswith("app_"):
                has_app_tag = True
        if has_app_tag:
            app_tag_contacts += 1
        if cp.get(config.PROP_DISTINCT_ID):
            distinct_id_set += 1

    print(f"OmniSend contacts scanned: {total}")
    print(f"  with an app-ish tag (app:* or app_*): {app_tag_contacts}")
    print(f"  with {config.PROP_DISTINCT_ID} set: {distinct_id_set}")
    print("  app/adoption-related tag counts:")
    for tag, n in tag_counts.most_common():
        low = tag.lower()
        if low.startswith("app") or "install" in low or "signup" in low or "adopt" in low:
            print(f"    {tag!r}: {n}")
    return 0


def _run_sync(args, fn_name: str) -> int:
    mode = _effective_mode(args.live)
    require_posthog = (
        fn_name in ("run_interaction", "run_all")
        and config.interaction_source() == "posthog"
    )
    try:
        config.validate(require_render=True, require_posthog=require_posthog)
    except config.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    if args.live and not config.external_writes_enabled():
        print("Refusing --live while DRY_RUN is true. Set DRY_RUN=false to write.")
        return 1
    print(f"Running {args.command} in mode={mode} "
          f"(force={args.force}, max_contacts={args.max_contacts}).")

    kwargs = dict(
        live=args.live,
        force=args.force,
        max_contacts=args.max_contacts,
        write_findings=not args.no_findings,
    )
    if fn_name == "run_membership":
        report, _ = pipeline.run_membership(**kwargs)
    elif fn_name == "run_interaction":
        report, _ = pipeline.run_interaction(since_days=args.since_days, **kwargs)
    else:
        report, _ = pipeline.run_all(since_days=args.since_days, **kwargs)
    return 1 if report.errors else 0


def cmd_membership(args) -> int:
    return _run_sync(args, "run_membership")


def cmd_interaction(args) -> int:
    return _run_sync(args, "run_interaction")


def cmd_run(args) -> int:
    return _run_sync(args, "run_all")


# ----------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bridge", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def add_sync_flags(sp) -> None:
        sp.add_argument("--live", action="store_true",
                        help="apply writes (also needs DRY_RUN=false)")
        sp.add_argument("--force", action="store_true",
                        help="bypass the max-diff guardrail (e.g. initial backfill)")
        sp.add_argument("--max-contacts", type=int, default=None,
                        help="cap OmniSend contacts scanned (testing)")
        sp.add_argument("--no-findings", action="store_true",
                        help="don't append the run to FINDINGS.md")

    sp = sub.add_parser("validate", help="check config; print effective mode")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("probe", help="Phase 0 join validation (read-only)")
    sp.add_argument("--sample", type=int, default=10)
    sp.add_argument("--since-days", type=int, default=90)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("baseline", help="Phase 0 OmniSend snapshot (read-only)")
    sp.add_argument("--max-contacts", type=int, default=None)
    sp.set_defaults(func=cmd_baseline)

    sp = sub.add_parser("membership", help="Phase 2 Render -> OmniSend membership sync")
    add_sync_flags(sp)
    sp.set_defaults(func=cmd_membership)

    sp = sub.add_parser("interaction", help="Phase 3 PostHog -> OmniSend interaction sync")
    add_sync_flags(sp)
    sp.add_argument("--since-days", type=int, default=None)
    sp.set_defaults(func=cmd_interaction)

    sp = sub.add_parser("run", help="full sync (membership + interaction)")
    add_sync_flags(sp)
    sp.add_argument("--since-days", type=int, default=None)
    sp.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
