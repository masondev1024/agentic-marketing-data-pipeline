-- :version is bound by psycopg, not interpolated from a question.
-- Clock comparisons are literal source wall times, not verified UTC business attribution.
INSERT INTO olist_mart.seller_funnel
WITH seller_orders AS (
    SELECT i.seller_id, count(DISTINCT i.order_id) AS order_count,
           min(o.purchased_at_source) AS first_order,
           max(o.purchased_at_source) AS last_order,
           count(DISTINCT i.order_id) FILTER (
               WHERE o.purchased_at_source >= d.won_at_source) AS post_orders,
           count(DISTINCT i.order_id) FILTER (
               WHERE o.purchased_at_source < d.won_at_source) AS pre_orders,
           min(o.purchased_at_source) FILTER (
               WHERE o.purchased_at_source >= d.won_at_source) AS first_post_order
    FROM olist_core.items i
    JOIN olist_core.orders o USING (version_id,order_id)
    JOIN olist_core.deals d USING (version_id,seller_id)
    WHERE i.version_id = %(version)s
    GROUP BY i.seller_id
)
SELECT d.version_id,d.mql_id,d.seller_id,l.origin,d.won_at_source,
       CASE WHEN s.seller_id IS NULL THEN 'unlinked_unknown' ELSE 'linked_sample' END,
       CASE WHEN s.seller_id IS NULL THEN NULL ELSE coalesce(o.order_count,0) END,
       o.first_order,o.last_order,
       CASE WHEN s.seller_id IS NULL THEN NULL ELSE coalesce(o.post_orders,0) END,
       CASE WHEN s.seller_id IS NULL THEN NULL ELSE coalesce(o.pre_orders,0) END,
       o.first_post_order,'naive_source_clock_unverified','coverage_and_timezone_unverified'
FROM olist_core.deals d
JOIN olist_core.leads l USING (version_id,mql_id)
LEFT JOIN olist_core.sellers s USING (version_id,seller_id)
LEFT JOIN seller_orders o USING (seller_id)
WHERE d.version_id = %(version)s;

INSERT INTO olist_mart.channel_funnel
SELECT l.version_id,
       CASE WHEN l.origin IS NULL THEN 'missing:' ELSE 'origin:' || l.origin END,
       l.origin,count(*),count(s.mql_id),
       count(*) FILTER (WHERE s.coverage_state = 'linked_sample'),
       count(*) FILTER (WHERE s.coverage_state = 'unlinked_unknown')
FROM olist_core.leads l
LEFT JOIN olist_mart.seller_funnel s USING (version_id,mql_id)
WHERE l.version_id = %(version)s
GROUP BY l.version_id,l.origin;
