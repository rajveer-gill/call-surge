"""Did the trial actually convert, and did we record it?

The webhook that records conversions once crashed on an UnboundLocalError and
returned 200 anyway, so Stripe saw success while nothing was written. That is
fixed, but no real conversion has exercised the fix. This compares what Stripe
believes against what our backend believes, because the failure mode is precisely
the two disagreeing while everything looks healthy.

Run:  py backend/scripts/check_trial_conversion.py
Reads only. Never writes to Stripe.
"""
import io
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SUB_ID = os.getenv("CHECK_SUB_ID", "sub_1U6oRADtrnw76kTJeM2U43KF")
EXPECT_ACCT = os.getenv("CHECK_ACCT", "acct_1TOm3ZDtrnw76kTJ")  # staging sandbox


def _key() -> str:
    env = io.open(BACKEND / ".env", encoding="utf-8", errors="replace").read()
    m = re.search(r"^STRIPE_SECRET_KEY=(\S+)", env, re.M)
    if not m:
        sys.exit("STRIPE_SECRET_KEY not found in backend/.env")
    return m.group(1)


def _ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else None


def main() -> int:
    import stripe

    stripe.api_key = _key()
    acct = stripe.Account.retrieve().to_dict()["id"]
    # Guard rather than trust: the sandbox and main-account keys share a prefix.
    if acct != EXPECT_ACCT:
        sys.exit(f"refusing to run: key belongs to {acct}, expected {EXPECT_ACCT}")
    print(f"account {acct} (read only)\n")

    s = stripe.Subscription.retrieve(SUB_ID).to_dict()
    status = s["status"]
    print(f"stripe subscription {SUB_ID}")
    print(f"  status    : {status}")
    print(f"  trial_end : {_ts(s.get('trial_end'))}")
    for it in (s.get("items") or {}).get("data", []):
        pr = it.get("price") or {}
        print(f"  qty {it.get('quantity')} x ${(pr.get('unit_amount') or 0)/100:.2f}"
              f"  ({pr.get('nickname') or pr.get('id')})")

    print("\nverdict:")
    if status == "trialing":
        print(f"  still trialing — converts {_ts(s.get('trial_end'))}. Re-run after that.")
        return 0
    if status == "active":
        print("  Stripe says ACTIVE. Now confirm OUR side recorded it:")
        print("  the org's subscription_status must read 'active', not 'trialing'.")
        print("  A mismatch is the exact bug this check exists for — the webhook")
        print("  returning 200 while writing nothing.")
        print(f"  org_id: {(s.get('metadata') or {}).get('org_id')}")
        return 0
    print(f"  unexpected status {status!r} — investigate before drawing conclusions.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
