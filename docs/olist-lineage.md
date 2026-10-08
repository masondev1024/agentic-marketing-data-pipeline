# Olist pipeline dependency context

`llm_ops_lab.olist.lineage.pipeline_context(version_evidence, run_history)` builds a small,
fixed dependency graph for the checked-in Olist PostgreSQL pipeline. It is intended to help an
operator locate a failed run boundary and choose safe read-only checks. It is not a runtime
lineage catalog: the graph comes from `olist/pipeline.py`, `olist/schema.sql`, and
`olist/transform.sql`, and every node is marked `runtime_state: not_observed`.

The graph follows this versioned path:

```text
source validation → Bronze freeze → core copy/reconcile → mart build/reconcile → publication
```

The code validates source contracts before freezing validated files and their replay manifest.
It then writes version-keyed core tables, analyzes the loaded tables, builds seller and channel
marts, reconciles row counts and funnel grain, and updates the publication pointer in the same
transaction as the successful run receipt. The `core_copy_reconcile` and `mart_build` nodes are
dependency stages, not separate runtime receipts: some reconciliation checks run after the mart
statements. The graph includes only dependencies; it does not claim that a node ran or passed.

The graph is bound to the `version_id` and `transform_sha256` in version evidence. The API should
include `active_published_version_id` captured in the same repeatable-read transaction. If that
pointer disagrees with the selected version, the function abstains with
`active_version_conflict`; it cannot safely call that version active. Run history is already
bounded to the newest five rows. The function rejects a larger input rather than choosing a
possibly incorrect subset.

Each result carries `lineage_map_version` and SHA-256 digests for `schema.sql`, `transform.sql`,
`pipeline.py`, and `validation.py`. It recomputes the same composite transform hash used by
`pipeline.transform_hash()`. If this current hash differs from version evidence, the function
returns `lineage_source_hash_mismatch` and no incident localization. This keeps a current source
map from being applied to an older published transformation whose steps may have differed.

For a failed run, the journal's `step` is mapped to its nearest stage boundary, and only later
stages are listed as candidate affected downstream nodes. An unknown step leaves localization
uncertain and lists the full graph as candidates. This is a dependency estimate, not a claim that
each listed stage is damaged. The newest failed row in the supplied history is reported; `running`
and `abandoned` rows remain distinct and are never treated as failed. The raw run-history evidence
should remain attached separately so a reader can inspect all five rows and provenance.

The failed run's `version_id` may differ from the selected published version, and run history does
not include its transform hash. Candidate stages therefore express the fixed dependency shape;
they do not prove that the failed version used this map.

`error_code` is an observation. In particular, `QueryCanceled` does not establish whether a
statement timeout, manual cancellation, resource pressure, or another condition caused the
failure. The result therefore keeps `diagnosis.root_cause` at `unknown`, does not invent node
completion or retry counts, and emits a short list of bounded read-only checks. A true
quality-passed version whose ID matches the same-snapshot publication pointer is reported as
`active_published_version_available`. `distinct_published_version_available` is known only when
the failed run has a valid version ID; it is true when that ID differs from the active version and
false when it is the same. When the run version is absent or malformed, distinct availability is
unknown. The compatibility field `last_good_preserved` remains `null`: a snapshot after a failure
cannot prove the publication pointer was unchanged across that earlier incident.

Recovery remains an operator decision. First read the exact run receipt, validation report,
version evidence, and retained Bronze hashes. Inspect recorded core row-count and mart
reconciliation checks in the run receipt and quality report. The `olist_reader` role has access to
the mart schema, not `olist_core` or `olist_ops`; do not present it as able to inspect core tables.
Confirm the publication pointer in a fresh consistent snapshot. After identifying and correcting the cause, an operator may manually rerun the existing
idempotent publisher; this lineage helper never retries, updates rows, changes the pointer, or
performs recovery automatically. A rerun is not a substitute for understanding a failed
`QueryCanceled` incident.

This is dependency-graph context for one fixed batch pipeline. It is not a knowledge graph, does
not extract relationships from model-generated text, and is not a durable LLM checkpoint or
execution trace. It adds no database tables and performs no database access itself.
