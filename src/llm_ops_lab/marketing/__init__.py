"""Versioned synthetic marketing data product; no model or external side effects."""

from llm_ops_lab.marketing.pipeline import process_spend_snapshots, publish_dataset

__all__ = ["process_spend_snapshots", "publish_dataset"]
