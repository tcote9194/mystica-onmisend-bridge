"""Mystica Tagging Bridge — Render + PostHog -> OmniSend identity & tagging.

A desired-state reconciler: each run pulls the Render roster + PostHog aggregates
+ current OmniSend contacts, computes the desired ``app:*`` tag / custom-property
set per email, diffs it against what's live, and applies only the delta. Idempotent
by construction; dry-run by default. See EXECUTION_PLAN.md for the full design.
"""

__version__ = "0.1.0"
