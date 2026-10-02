"""Outbound phone campaign: dial every eligible customer, one call at a time.

All calls go to ONE number you control (--to), never to the fictional numbers on file.

    python -m scripts.run_campaign --to +91XXXXXXXXXX            # respects calling window + attempts + DNC
    python -m scripts.run_campaign --to +91XXXXXXXXXX --force    # ignore the calling window (demo at night)
    python -m scripts.run_campaign --to +91XXXXXXXXXX --only cust_004
"""
from __future__ import annotations

import argparse
import sys
import time

from app import db, policy, vapi_client


def wait_for_end(call_id: str, timeout_s: int = 300) -> str:
    deadline = time.time() + timeout_s
    status = "unknown"
    while time.time() < deadline:
        call = vapi_client._request("GET", f"/call/{call_id}")
        status = call.get("status", status)
        if status == "ended":
            return call.get("endedReason", "ended")
        time.sleep(3)
    return f"timeout ({status})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="E.164 number you own or have permission to call")
    ap.add_argument("--force", action="store_true", help="ignore the calling-hours window")
    ap.add_argument("--only", help="customer id to dial, e.g. cust_004")
    args = ap.parse_args()

    db.init()
    customers = [c for c in db.list_customers() if not args.only or c["id"] == args.only]
    for c in customers:
        ok, why = policy.eligibility(c, ignore_window=args.force)
        if not ok:
            print(f"SKIP  {c['id']} {c['name']:<16} {why}")
            continue
        try:
            res = vapi_client.create_phone_call(c, args.to)
        except vapi_client.VapiError as e:
            print(f"ERROR {c['id']} {c['name']:<16} {e}", file=sys.stderr)
            continue
        db.upsert_call(res["id"], customer_id=c["id"], channel="phone", status=res.get("status", "queued"),
                       started_at=db.now_iso())
        print(f"DIAL  {c['id']} {c['name']:<16} call {res['id']} ... ", end="", flush=True)
        print(wait_for_end(res["id"]))
        time.sleep(5)   # breathing room between calls to the same handset
    print("\nDone. Open the dashboard for outcomes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
