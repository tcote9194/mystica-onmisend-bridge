from __future__ import annotations

from bridge.identity import IdentityBridge, normalize_email, normalize_user_id


def test_normalize_email_lowercases_and_trims():
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"
    assert normalize_email(None) == ""
    assert normalize_email("") == ""


def test_normalize_user_id_trims_but_preserves_case():
    assert normalize_user_id("  Ab12 ") == "Ab12"
    assert normalize_user_id(None) == ""


def test_bridge_resolves_known_and_drops_unknown(roster):
    bridge = IdentityBridge(roster)
    # u3 has no email -> excluded; u1/u2/u4 present.
    assert len(bridge) == 3
    assert bridge.email_for("u1") == "alice@example.com"
    assert bridge.email_for("u2") == "bob@example.com"
    assert bridge.email_for("u3") is None  # no email
    assert bridge.email_for("nope") is None


def test_resolve_emails_returns_only_resolved(roster):
    bridge = IdentityBridge(roster)
    out = bridge.resolve_emails(["u1", "u3", "unknown", "u4"])
    assert out == {"u1": "alice@example.com", "u4": "carol@example.com"}


def test_first_good_email_wins():
    from bridge.models import RenderUser

    bridge = IdentityBridge([
        RenderUser("dup", "first@example.com"),
        RenderUser("dup", "second@example.com"),
    ])
    assert bridge.email_for("dup") == "first@example.com"
