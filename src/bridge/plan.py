"""Diff desired state against current OmniSend state -> a ChangePlan.

The reconciler's heart. For each desired contact that EXISTS in OmniSend:
  - tags to add  = desired.tags - current.tags   (add-only; never a removal)
  - props to set = desired props whose value differs from current (string-compared)
A desired email with NO OmniSend contact is counted as unresolved (logged, never a
write — the bridge tags existing contacts; it does not create them). Empty deltas
are dropped, so a fully-reconciled population yields an empty plan (idempotency is
structural).
"""

from __future__ import annotations

import logging

from bridge.models import ChangePlan, ContactChange, CurrentContact, DesiredContact

log = logging.getLogger("bridge.plan")


def _prop_differs(desired_value: object, current_value: object) -> bool:
    """True when a property needs writing. Compared as strings so 5 == "5" (OmniSend
    stores custom properties as strings) doesn't churn a needless write every run."""
    if current_value is None:
        return True
    return str(desired_value) != str(current_value)


def diff_contact(
    desired: DesiredContact, current: CurrentContact
) -> ContactChange:
    """The minimal delta to bring ``current`` in line with ``desired``."""
    add_tags = tuple(sorted(t for t in desired.tags if t not in current.tags))
    set_props = {
        k: v
        for k, v in desired.props.items()
        if v is not None and _prop_differs(v, current.props.get(k))
    }
    return ContactChange(
        email=desired.email,
        contact_id=current.contact_id,
        add_tags=add_tags,
        set_props=set_props,
    )


def build_plan(
    desired: dict[str, DesiredContact],
    current: dict[str, CurrentContact],
    *,
    audience_size: int | None = None,
) -> ChangePlan:
    """Build the change plan.

    ``audience_size`` is the guardrail denominator (defaults to the number of live
    OmniSend contacts). Desired emails with no OmniSend contact are tallied as
    ``unresolved_emails`` — a first-class metric, never a silent drop.
    """
    changes: list[ContactChange] = []
    matched = 0
    unresolved = 0
    for email, d in desired.items():
        cur = current.get(email)
        if cur is None:
            unresolved += 1
            continue
        matched += 1
        change = diff_contact(d, cur)
        if not change.is_empty:
            changes.append(change)

    plan = ChangePlan(
        changes=changes,
        desired_count=len(desired),
        matched_count=matched,
        unresolved_emails=unresolved,
        audience_size=audience_size if audience_size is not None else len(current),
    )
    log.info(
        "plan: %d changes over %d matched (%d unresolved, %.1f%% of audience)",
        plan.touched, matched, unresolved, plan.diff_fraction * 100,
    )
    return plan
