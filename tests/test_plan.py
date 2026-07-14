from __future__ import annotations

from bridge import config
from bridge.models import CurrentContact, DesiredContact
from bridge.plan import build_plan, diff_contact


def _current(email, tags=(), props=None):
    return CurrentContact(
        contact_id=f"cid-{email}",
        email=email,
        tags=frozenset(tags),
        props=props or {},
    )


def test_diff_adds_missing_tags_only():
    desired = DesiredContact("a@x.com", tags={config.TAG_INSTALL, config.TAG_SIGNUP})
    current = _current("a@x.com", tags={config.TAG_INSTALL})
    change = diff_contact(desired, current)
    assert change.add_tags == (config.TAG_SIGNUP,)  # install already present, not re-added


def test_diff_sets_only_changed_props():
    desired = DesiredContact("a@x.com", props={config.PROP_CHATS: 5, config.PROP_LAST_SEEN: "2026-01-01"})
    current = _current("a@x.com", props={config.PROP_CHATS: "5"})  # equal as strings
    change = diff_contact(desired, current)
    # app_chats unchanged (5 == "5"), last_seen is new
    assert change.set_props == {config.PROP_LAST_SEEN: "2026-01-01"}


def test_fully_reconciled_yields_empty_plan():
    desired = {"a@x.com": DesiredContact("a@x.com", tags={config.TAG_INSTALL},
                                         props={config.PROP_INSTALLED_AT: "2026-01-01"})}
    current = {"a@x.com": _current("a@x.com", tags={config.TAG_INSTALL},
                                   props={config.PROP_INSTALLED_AT: "2026-01-01"})}
    plan = build_plan(desired, current)
    assert plan.touched == 0  # idempotency is structural


def test_unresolved_emails_counted_not_written():
    desired = {
        "a@x.com": DesiredContact("a@x.com", tags={config.TAG_INSTALL}),
        "ghost@x.com": DesiredContact("ghost@x.com", tags={config.TAG_INSTALL}),
    }
    current = {"a@x.com": _current("a@x.com")}
    plan = build_plan(desired, current)
    assert plan.matched_count == 1
    assert plan.unresolved_emails == 1
    assert plan.touched == 1  # only the matched contact is in the plan


def test_diff_fraction_uses_audience_size():
    desired = {f"{i}@x.com": DesiredContact(f"{i}@x.com", tags={config.TAG_INSTALL})
               for i in range(10)}
    current = {f"{i}@x.com": _current(f"{i}@x.com") for i in range(10)}
    plan = build_plan(desired, current, audience_size=100)
    assert plan.touched == 10
    assert abs(plan.diff_fraction - 0.10) < 1e-9
