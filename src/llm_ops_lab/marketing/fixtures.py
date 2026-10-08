"""Deterministic synthetic sources. No randomness, private inputs or inferred attribution."""

from copy import deepcopy
from datetime import timedelta

from llm_ops_lab.marketing.contracts import VERSION, day


def make_fixture(split: str = "development") -> dict:
    if split not in {"development", "acceptance"}:
        raise ValueError("unknown fixture split")
    start = "2026-10-04" if split == "development" else "2026-09-20"
    first = day(start)
    second = (first + timedelta(days=1)).isoformat()
    as_of = (first + timedelta(days=3)).isoformat() + "T12:00:00Z"
    accounts = ["alpha", "beta"]
    campaigns = [{"account_id": a, "campaign_id": c, "name": f"synthetic-{a}-{c}"}
                 for a, c in (("alpha", "c1"), ("alpha", "c2"), ("beta", "c1"))]
    spend = []
    for date_str in (start, second):
        for a, c, amount in (("alpha", "c1", 10000), ("alpha", "c2", 5000),
                             ("beta", "c1", 8000)):
            if date_str == second and c == "c2":
                amount = 0
            spend.append({"account_id": a, "campaign_id": c, "source": "spend",
                          "snapshot_id": f"{c}-{date_str}", "business_date": date_str,
                          "snapshot_at": date_str + "T15:00:00Z", "revision": 1,
                          "event_time": date_str + "T15:01:00Z",
                          "ingested_at": date_str + "T15:02:00Z", "spend_krw": amount})
    orders = []
    specs = [("alpha", "c1", "o1", start, 30000),
             ("alpha", "c1", "o2", start, 20000),
             ("alpha", "c2", "o3", start, 10000),
             ("alpha", "c1", "o4", second, 12000),
             ("alpha", "c2", "o5", second, 8000),
             ("beta", "c1", "o1", start, 16000),
             ("beta", "c1", "o2", second, 4000)]
    for a, c, oid, date_str, amount in specs:
        orders.append({"account_id": a, "campaign_id": c, "order_id": oid, "source": "orders",
                       "event_time": date_str + "T02:00:00Z",
                       "ingested_at": date_str + "T02:01:00Z", "revision": 1,
                       "gross_sales_krw": amount})
    refunds = [{"account_id": "alpha", "order_id": "o1", "refund_id": rid,
                "source": "refunds", "revision": 1, "refund_krw": amount,
                "event_time": second + "T03:00:00Z", "ingested_at": second + "T03:01:00Z"}
               for rid, amount in (("r1", 5000), ("r2", 2000))]
    coverage = [{"account_id": a, "business_date": d, "source": s,
                 "complete": True, "through_utc": as_of}
                for a in accounts for d in (start, second) for s in ("spend", "orders", "refunds")]
    return {"schema_version": VERSION, "fixture_revision": f"marketing-{split}.v1",
            "as_of": as_of, "date_start": start, "date_end": second, "accounts": accounts,
            "campaigns": campaigns, "spend_snapshots": spend, "orders": orders,
            "refunds": refunds, "coverage": coverage}


def scenario(base: dict, name: str) -> dict:
    result = deepcopy(base)
    result["fixture_revision"] = base["fixture_revision"] + "." + name
    second = result["date_end"]
    target = next(r for r in result["spend_snapshots"] if r["account_id"] == "alpha"
                  and r["campaign_id"] == "c1" and r["business_date"] == second)
    if name in {"normal", "observed_decline"}:
        return result
    if name == "duplicate":
        for table in ("campaigns", "spend_snapshots", "orders", "refunds"):
            result[table] += deepcopy(result[table])
    elif name == "corrected_spend":
        result["spend_snapshots"].append(target | {"revision": 2, "spend_krw": 12000,
                                                 "event_time": result["as_of"],
                                                 "ingested_at": result["as_of"]})
    elif name == "conflict":
        result["spend_snapshots"].append(target | {"spend_krw": 9999})
    elif name in {"late_order", "late_refund"}:
        late_time = (day(result["date_start"]) + timedelta(days=4)).isoformat() + "T12:00:00Z"
        if name == "late_order":
            result["orders"].append(result["orders"][3] | {
                "order_id": "late1", "gross_sales_krw": 6000, "ingested_at": late_time,
            })
        else:
            result["refunds"].append(result["refunds"][0] | {
                "refund_id": "late1", "refund_krw": 3000, "ingested_at": late_time,
            })
        # The fixture first excludes this delivery; advancing as_of enables the backfill.
    elif name == "missing_mapping":
        result["orders"][0]["campaign_id"] = "unknown"
    elif name == "incomplete":
        result["coverage"] = [r for r in result["coverage"] if not (
            r["account_id"] == "alpha" and r["business_date"] == second and r["source"] == "orders"
        )]
    elif name == "schema_error":
        target["spend_krw"] = "10000"
    elif name == "zero_spend":
        target["spend_krw"] = 0
    else:
        raise ValueError("unknown scenario")
    return result
