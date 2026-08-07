"""The customer-side Relium Collector.

This package runs inside the customer's environment, not inside Relium. It
closes the one gap between a review that needs production evidence and a
review that has it:

    Relium creates a targeted collection request
      -> the collector reads it
      -> queries the customer's warehouse for ONLY the requested metadata
      -> constructs a metadata snapshot
      -> submits it to Relium's public API
      -> Relium's existing worker recomputes the review

Everything on the Relium side of that flow already existed. This package is
the customer side of it, and deliberately nothing more.

The boundaries are not incidental:

  * Relium never sends SQL. The collector generates every query locally from a
    small, closed signal vocabulary, so a compromised or hostile control plane
    cannot make the collector run arbitrary statements against a warehouse.
  * Only the relations, columns and signals named in the request are touched.
    There is no discovery pass and no full warehouse scan.
  * Nothing that leaves the customer's environment contains a row, a cell, a
    credential or a query - only aggregate metadata about shape and quality.
"""
from agent.collector.config import CollectorConfig, CollectorConfigError
from agent.collector.runner import CollectionOutcome, run_collection

__all__ = [
    "CollectorConfig",
    "CollectorConfigError",
    "CollectionOutcome",
    "run_collection",
]
