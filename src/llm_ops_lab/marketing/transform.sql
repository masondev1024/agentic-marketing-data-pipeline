-- v1: every join below has one row per declared join key.
CREATE TABLE gold_campaign AS
WITH refund_by_order AS (
    SELECT account_id, order_id, SUM(refund_krw)::BIGINT AS refund_krw
    FROM silver_refunds GROUP BY account_id, order_id
), sales AS (
    SELECT o.account_id, o.campaign_id, o.business_date,
           COUNT(*)::BIGINT AS order_count,
           SUM(o.gross_sales_krw)::BIGINT AS gross_sales_krw,
           SUM(COALESCE(r.refund_krw, 0))::BIGINT AS refund_krw
    FROM silver_orders o LEFT JOIN refund_by_order r USING (account_id, order_id)
    GROUP BY o.account_id, o.campaign_id, o.business_date
), grid AS (
    SELECT c.account_id, c.campaign_id, c.name, d.business_date
    FROM silver_campaigns c CROSS JOIN business_days d
), amounts AS (
    SELECT g.*, COALESCE(s.spend_krw, 0)::BIGINT AS spend_krw,
           COALESCE(o.order_count, 0)::BIGINT AS order_count,
           COALESCE(o.gross_sales_krw, 0)::BIGINT AS gross_sales_krw,
           COALESCE(o.refund_krw, 0)::BIGINT AS refund_krw,
           q.complete, q.through_utc
    FROM grid g
    LEFT JOIN silver_spend s USING (account_id, campaign_id, business_date)
    LEFT JOIN sales o USING (account_id, campaign_id, business_date)
    JOIN quality_daily q USING (account_id, business_date)
)
SELECT *, (gross_sales_krw - refund_krw)::BIGINT AS net_attributed_sales_krw,
       CASE WHEN spend_krw > 0 THEN
           (gross_sales_krw - refund_krw)::DOUBLE / spend_krw ELSE NULL END AS observed_roas,
       CASE WHEN complete AND spend_krw > 0 THEN
           (gross_sales_krw - refund_krw)::DOUBLE / spend_krw ELSE NULL END AS roas
FROM amounts ORDER BY account_id, business_date, campaign_id;

CREATE TABLE gold_account AS
WITH amounts AS (
    SELECT account_id, business_date, SUM(spend_krw)::BIGINT AS spend_krw,
           SUM(order_count)::BIGINT AS order_count,
           SUM(gross_sales_krw)::BIGINT AS gross_sales_krw,
           SUM(refund_krw)::BIGINT AS refund_krw,
           SUM(net_attributed_sales_krw)::BIGINT AS net_attributed_sales_krw,
           BOOL_AND(complete) AS complete, MIN(through_utc) AS through_utc
    FROM gold_campaign GROUP BY account_id, business_date
)
SELECT *, CASE WHEN spend_krw > 0 THEN
           net_attributed_sales_krw::DOUBLE / spend_krw ELSE NULL END AS observed_roas,
       CASE WHEN complete AND spend_krw > 0 THEN
           net_attributed_sales_krw::DOUBLE / spend_krw ELSE NULL END AS roas
FROM amounts ORDER BY account_id, business_date;
