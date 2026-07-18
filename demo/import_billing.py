"""Lane-2 demo: import 'actual' cloud costs (simulated AWS CUR export) and
reconcile against Guardian's live metered estimates.

In production this reads a real AWS Cost & Usage Report filtered by
cost-allocation tags (swarm=<id>). Here, sample_cur.csv plays that role.
Usage: python import_billing.py [sample_cur.csv]
"""
from __future__ import annotations

import csv
import os
import sys

import httpx

BASE = os.environ.get("GUARDIAN_URL", "http://localhost:8090")
KEY = os.environ.get("GUARDIAN_API_KEY", "guardian-dev-key")


def main(path: str = "sample_cur.csv") -> None:
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({"swarm_id": r["swarm_id"], "category": r["category"],
                         "cost_usd": float(r["cost_usd"]),
                         "source": r.get("source", "aws_cur"),
                         "period": r.get("period", "")})
    with httpx.Client(timeout=10, trust_env=False) as c:
        resp = c.post(f"{BASE}/v1/billing/import", json=rows,
                      headers={"X-Guardian-Key": KEY})
        resp.raise_for_status()
        print(f"imported {resp.json()['imported']} billing rows")
        rec = c.get(f"{BASE}/v1/billing/reconciliation").json()
    for s in rec:
        print(f"\nswarm {s['swarm_id']}: metered ${s['metered_total']} "
              f"vs actual ${s['actual_total']}")
        for cat in s["categories"]:
            d = cat["delta_pct"]
            print(f"  {cat['category']:8} metered ${cat['metered_usd']:<8} "
                  f"actual ${cat['actual_usd']:<8} "
                  f"delta {d if d is not None else 'n/a'}%")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample_cur.csv")
