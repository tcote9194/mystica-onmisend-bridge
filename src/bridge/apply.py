"""Execute a ChangePlan against OmniSend — rate-limited, per-contact fault-isolated.

The write client is already gated (dry-run -> no HTTP), so :func:`apply_plan` runs
in every mode; in dry-run it exercises the exact same path and returns synthetic
results. A per-contact failure is caught, counted, and does NOT abort the run (one
bad contact must not strand the rest); the error count surfaces in the report and
trips a Slack alert.
"""

from __future__ import annotations

import logging
import time

from bridge import config
from bridge.models import ChangePlan
from bridge.omnisend import OmniSendClient, OmnisendAuthError

log = logging.getLogger("bridge.apply")


class GuardrailTripped(RuntimeError):
    """Raised when a plan would change more than ``MAX_DIFF_FRACTION`` of the audience
    and ``force`` was not passed. Protects against a bad pull mass-mutating tags."""


def apply_plan(
    plan: ChangePlan,
    client: OmniSendClient | None = None,
    *,
    force: bool = False,
    sleep: float = 0.0,
) -> tuple[int, int, int]:
    """Apply every change in ``plan``. Returns ``(applied, created, errors)``.

    Trips :class:`GuardrailTripped` BEFORE any write if the plan is too large and
    ``force`` is False. An auth error propagates immediately (the key is bad — retry
    won't help and the whole run should stop); any other per-contact error is caught,
    logged, and counted.
    """
    if not force and plan.diff_fraction > config.max_diff_fraction():
        raise GuardrailTripped(
            f"plan touches {plan.touched} of {plan.audience_size} contacts "
            f"({plan.diff_fraction:.1%} > {config.max_diff_fraction():.0%}); "
            f"re-run with --force if this is intended (e.g. the initial backfill)"
        )

    client = client or OmniSendClient()
    applied = 0
    errors = 0
    for change in plan.changes:
        try:
            client.apply_change(
                change.contact_id,
                add_tags=list(change.add_tags),
                set_props=dict(change.set_props),
            )
            applied += 1
        except OmnisendAuthError:
            raise  # bad key — stop the whole run
        except Exception as exc:  # per-contact fault isolation
            errors += 1
            log.warning("apply failed for %s: %s", change.email, exc)
        if sleep:
            time.sleep(sleep)

    # Create-and-tag the missing contacts (only present when --create-missing was set).
    created = 0
    for c in plan.creates:
        try:
            client.create_contact(
                c.email, tags=list(c.tags), props=dict(c.props), first_name=c.first_name,
            )
            created += 1
        except OmnisendAuthError:
            raise
        except Exception as exc:
            errors += 1
            log.warning("create failed for %s: %s", c.email, exc)
        if sleep:
            time.sleep(sleep)

    log.info("applied %d changes, created %d contacts (%d errors)", applied, created, errors)
    return applied, created, errors
