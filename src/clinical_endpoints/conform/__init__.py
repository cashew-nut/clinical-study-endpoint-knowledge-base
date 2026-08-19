"""The conforming pipeline (build-order step 3): normalize -> syntactic match ->
semantic fallback -> review queue, plus the threshold and timepoint parsers.

Every matching rule this package implements is read at runtime from the
`vocab.*` tables `endpoints vocab validate` writes -- never by re-parsing
vocab/*.yaml, and never by hardcoding a term id, synonym, or pattern. See
`vocab/matching.yaml` for the contract this package implements, and
`vocab/README.md` for the judgment calls behind each vocabulary file.
"""
