"""The conforming pipeline: normalise -> syntactic match -> semantic fallback ->
review queue, plus the threshold and timepoint parsers.

Every matching rule is read from the `vocab.*` tables `vocab validate` writes,
never from the YAML directly. See `vocab/matching.yaml` for the contract.
"""
