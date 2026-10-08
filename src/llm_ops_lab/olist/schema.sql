CREATE SCHEMA IF NOT EXISTS olist_ops;
CREATE SCHEMA IF NOT EXISTS olist_core;
CREATE SCHEMA IF NOT EXISTS olist_mart;

CREATE TABLE IF NOT EXISTS olist_ops.migrations (
    name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS olist_ops.versions (
    version_id text PRIMARY KEY,
    dataset_id text NOT NULL,
    contract_version text NOT NULL,
    transform_sha256 text NOT NULL,
    source_manifest jsonb NOT NULL,
    quality_report jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS olist_ops.publication (
    product text PRIMARY KEY CHECK (product = 'olist'),
    version_id text NOT NULL REFERENCES olist_ops.versions(version_id)
);
CREATE TABLE IF NOT EXISTS olist_ops.runs (
    run_id uuid PRIMARY KEY,
    version_id text,
    status text NOT NULL CHECK (status IN ('running','published','reused','failed','abandoned')),
    step text NOT NULL,
    error_code text,
    validation jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS olist_core.leads (
    version_id text NOT NULL REFERENCES olist_ops.versions(version_id),
    mql_id text NOT NULL,
    first_contact_date date NOT NULL,
    origin text,
    PRIMARY KEY (version_id, mql_id)
);
CREATE TABLE IF NOT EXISTS olist_core.sellers (
    version_id text NOT NULL REFERENCES olist_ops.versions(version_id),
    seller_id text NOT NULL,
    PRIMARY KEY (version_id, seller_id)
);
CREATE TABLE IF NOT EXISTS olist_core.deals (
    version_id text NOT NULL,
    mql_id text NOT NULL,
    seller_id text NOT NULL,
    won_at_source timestamp NOT NULL,
    PRIMARY KEY (version_id, mql_id),
    UNIQUE (version_id, seller_id),
    FOREIGN KEY (version_id,mql_id) REFERENCES olist_core.leads(version_id,mql_id)
    -- No FK to commerce sellers: these public samples have different coverage.
);
CREATE TABLE IF NOT EXISTS olist_core.orders (
    version_id text NOT NULL REFERENCES olist_ops.versions(version_id),
    order_id text NOT NULL,
    order_status text NOT NULL,
    purchased_at_source timestamp NOT NULL,
    PRIMARY KEY (version_id,order_id)
);
CREATE TABLE IF NOT EXISTS olist_core.items (
    version_id text NOT NULL,
    order_id text NOT NULL,
    order_item_id integer NOT NULL CHECK (order_item_id > 0),
    seller_id text NOT NULL,
    price_source numeric(18,2) NOT NULL CHECK (price_source >= 0 AND price_source <> 'NaN'),
    freight_source numeric(18,2) NOT NULL CHECK (freight_source >= 0 AND freight_source <> 'NaN'),
    PRIMARY KEY (version_id,order_id,order_item_id),
    FOREIGN KEY (version_id,order_id) REFERENCES olist_core.orders(version_id,order_id),
    FOREIGN KEY (version_id,seller_id) REFERENCES olist_core.sellers(version_id,seller_id)
);
CREATE TABLE IF NOT EXISTS olist_mart.seller_funnel (
    version_id text NOT NULL REFERENCES olist_ops.versions(version_id),
    mql_id text NOT NULL,
    seller_id text NOT NULL,
    origin text,
    won_at_source timestamp NOT NULL,
    coverage_state text NOT NULL CHECK (coverage_state IN ('linked_sample','unlinked_unknown')),
    observed_order_count bigint,
    first_order_at_source timestamp,
    last_order_at_source timestamp,
    post_contract_order_count_source_clock bigint,
    pre_contract_order_count_source_clock bigint,
    first_post_contract_order_at_source timestamp,
    clock_basis text NOT NULL CHECK (clock_basis = 'naive_source_clock_unverified'),
    window_30d_state text NOT NULL CHECK (window_30d_state = 'coverage_and_timezone_unverified'),
    PRIMARY KEY (version_id,mql_id),
    CHECK ((coverage_state = 'unlinked_unknown' AND observed_order_count IS NULL)
        OR (coverage_state = 'linked_sample' AND observed_order_count >= 0))
);
CREATE TABLE IF NOT EXISTS olist_mart.channel_funnel (
    version_id text NOT NULL REFERENCES olist_ops.versions(version_id),
    channel_key text NOT NULL,
    origin text,
    lead_count bigint NOT NULL,
    contract_count bigint NOT NULL,
    linked_contract_count bigint NOT NULL,
    unlinked_contract_count bigint NOT NULL,
    PRIMARY KEY (version_id,channel_key),
    CHECK (lead_count >= contract_count),
    CHECK (contract_count = linked_contract_count + unlinked_contract_count)
);
CREATE OR REPLACE VIEW olist_mart.current_version AS
    SELECT version_id FROM olist_ops.publication WHERE product = 'olist';
CREATE OR REPLACE VIEW olist_mart.version_evidence AS
    SELECT version_id,dataset_id,contract_version,transform_sha256,quality_report,published_at
    FROM olist_ops.versions;
CREATE OR REPLACE VIEW olist_mart.run_history AS
    SELECT run_id,version_id,status,step,error_code,started_at,finished_at
    FROM olist_ops.runs;
GRANT USAGE ON SCHEMA olist_mart TO olist_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA olist_mart TO olist_reader;
