"""Internal short-lived SQL reader; stdin is created by the trusted diagnostic host."""

import json
import sys

import duckdb

from llm_ops_lab.marketing.contracts import canonical

# SQL text and JSON field names are static; values and row limit are parameters.
METRICS_SQL = """
SELECT value->>'account_id' AS account_id, value->>'business_date' AS business_date,
       (value->>'spend_krw')::BIGINT AS spend_krw,
       (value->>'order_count')::BIGINT AS order_count,
       (value->>'gross_sales_krw')::BIGINT AS gross_sales_krw,
       (value->>'refund_krw')::BIGINT AS refund_krw,
       (value->>'net_attributed_sales_krw')::BIGINT AS net_attributed_sales_krw,
       (value->>'complete')::BOOLEAN AS complete,
       value->>'through_utc' AS through_utc,
       (value->>'observed_roas')::DOUBLE AS observed_roas,
       (value->>'roas')::DOUBLE AS roas
FROM json_each(?)
WHERE (value->>'account_id') = ? AND (value->>'business_date') BETWEEN ? AND ?
ORDER BY business_date LIMIT ?
"""


def query(payload):
    tool, scope = payload["tool"], payload["scope"]
    if tool not in {"metrics", "quality", "freshness", "definitions"}:
        raise ValueError("unauthorized tool")
    if tool == "definitions":
        return payload["definitions"]
    with duckdb.connect(":memory:", config={"threads": "1", "memory_limit": "128MB",
                                           "enable_external_access": "false",
                                           "max_temp_directory_size": "0B"}) as connection:
        cursor = connection.execute(METRICS_SQL, [json.dumps(payload["rows"]),
            scope["account_id"], scope["date_start"], scope["date_end"], payload["max_rows"] + 1])
        rows = [dict(zip([c[0] for c in cursor.description], r, strict=True))
                for r in cursor.fetchall()]
    if tool == "quality":
        return [{k: r[k] for k in ("account_id", "business_date", "complete")} for r in rows]
    if tool == "freshness":
        return {"as_of": payload["as_of"], "rows": [
            {k: r[k] for k in ("account_id", "business_date", "through_utc")} for r in rows]}
    return rows


if __name__ == "__main__":
    payload = json.loads(sys.stdin.buffer.read(1024 * 1024))
    sys.stdout.buffer.write(canonical(query(payload)))
