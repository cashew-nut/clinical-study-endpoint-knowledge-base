-- Clinical Study Endpoint Knowledge Base -- DuckDB schema.
--
-- Layout follows the three-layer model:
--   Layer A  vocabulary: axis, term, concept and their structure
--   Layer B  study specifications: study, study_outcome, endpoint_spec
--   Layer C  projection: usdm_projection
-- plus provenance tables that make every derived row attributable to the rule,
-- extractor and source record that produced it.
--
-- Structure is stored in long form (one row per axis) rather than as wide columns.
-- That is what lets a new axis be added without a migration, and what makes
-- "every endpoint whose reference is a population norm" a single indexed query.

-- ===========================================================================
-- Layer A: controlled vocabulary
-- ===========================================================================
CREATE TABLE IF NOT EXISTS axis (
    axis_id                 VARCHAR PRIMARY KEY,
    label                   VARCHAR NOT NULL,
    definition              VARCHAR NOT NULL,
    extensible              BOOLEAN NOT NULL,
    usdm_entity             VARCHAR,
    usdm_attribute          VARCHAR,
    usdm_codelist_c_code    VARCHAR
);

CREATE TABLE IF NOT EXISTS term (
    axis_id                 VARCHAR NOT NULL,
    term_id                 VARCHAR NOT NULL,
    label                   VARCHAR NOT NULL,
    definition              VARCHAR NOT NULL,
    synonyms                JSON,
    broader                 VARCHAR,
    attributes              JSON,
    status                  VARCHAR NOT NULL,
    notes                   VARCHAR,
    PRIMARY KEY (axis_id, term_id)
);

CREATE TABLE IF NOT EXISTS term_external_mapping (
    axis_id                 VARCHAR NOT NULL,
    term_id                 VARCHAR NOT NULL,
    system                  VARCHAR NOT NULL,
    code                    VARCHAR NOT NULL,
    display                 VARCHAR,
    system_version          VARCHAR,
    verified                BOOLEAN NOT NULL,
    verified_against        VARCHAR
);

CREATE TABLE IF NOT EXISTS concept (
    concept_id              VARCHAR PRIMARY KEY,
    label                   VARCHAR NOT NULL,
    abbreviation            VARCHAR,
    definition              VARCHAR NOT NULL,
    synonyms                JSON,
    therapeutic_areas       JSON,
    usdm_hints              JSON,
    status                  VARCHAR NOT NULL,
    notes                   VARCHAR,
    source_file             VARCHAR
);

-- One row per (concept, axis). The join table that carries the parameter model.
-- role is 'defining' when the axis is constitutive of the concept, 'default' when it
-- records the usual choice that a protocol may override without changing the concept.
CREATE TABLE IF NOT EXISTS concept_structure (
    concept_id              VARCHAR NOT NULL,
    axis_id                 VARCHAR NOT NULL,
    term_id                 VARCHAR NOT NULL,
    role                    VARCHAR NOT NULL,
    PRIMARY KEY (concept_id, axis_id)
);

CREATE TABLE IF NOT EXISTS concept_therapeutic_area (
    concept_id              VARCHAR NOT NULL,
    therapeutic_area        VARCHAR NOT NULL,
    PRIMARY KEY (concept_id, therapeutic_area)
);

CREATE TABLE IF NOT EXISTS concept_threshold (
    concept_id              VARCHAR PRIMARY KEY,
    kind                    VARCHAR NOT NULL,
    operator                VARCHAR NOT NULL,
    value_text              VARCHAR,
    value_num               DOUBLE,
    unit                    VARCHAR,
    applies_to              VARCHAR,
    note                    VARCHAR
);

CREATE TABLE IF NOT EXISTS concept_component (
    concept_id              VARCHAR NOT NULL,
    ordinal                 INTEGER NOT NULL,
    label                   VARCHAR NOT NULL,
    component_concept_id    VARCHAR,
    measurement_concept     VARCHAR,
    note                    VARCHAR,
    PRIMARY KEY (concept_id, ordinal)
);

CREATE TABLE IF NOT EXISTS concept_relation (
    concept_id              VARCHAR NOT NULL,
    related_concept_id      VARCHAR NOT NULL,
    relation                VARCHAR NOT NULL,
    note                    VARCHAR,
    PRIMARY KEY (concept_id, related_concept_id, relation)
);

CREATE TABLE IF NOT EXISTS concept_criteria (
    concept_id              VARCHAR NOT NULL,
    name                    VARCHAR NOT NULL,
    version                 VARCHAR,
    citation                VARCHAR,
    url                     VARCHAR
);

CREATE TABLE IF NOT EXISTS rule (
    rulepack_id             VARCHAR NOT NULL,
    rulepack_version        VARCHAR NOT NULL,
    rule_id                 VARCHAR NOT NULL,
    concept_id              VARCHAR NOT NULL,
    priority                INTEGER NOT NULL,
    confidence              DOUBLE NOT NULL,
    therapeutic_area_hint   VARCHAR,
    match_spec              JSON,
    asserts                 JSON,
    notes                   VARCHAR,
    PRIMARY KEY (rulepack_id, rulepack_version, rule_id)
);

-- ===========================================================================
-- Source data (bronze/silver)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS source_snapshot (
    snapshot_id             VARCHAR PRIMARY KEY,
    source                  VARCHAR NOT NULL,
    fetched_at              TIMESTAMP NOT NULL,
    request_params          JSON,
    record_count            BIGINT,
    content_hash            VARCHAR,
    file_path               VARCHAR
);

CREATE TABLE IF NOT EXISTS study (
    study_id                VARCHAR PRIMARY KEY,     -- NCT number for CT.gov
    source                  VARCHAR NOT NULL,
    brief_title             VARCHAR,
    official_title          VARCHAR,
    overall_status          VARCHAR,
    study_type              VARCHAR,
    phases                  JSON,
    enrollment              BIGINT,
    lead_sponsor            VARCHAR,
    sponsor_class           VARCHAR,
    conditions              JSON,
    therapeutic_areas       JSON,                    -- inferred, with evidence in study_ta_evidence
    start_date              VARCHAR,
    primary_completion_date VARCHAR,
    completion_date         VARCHAR,
    last_update_posted      VARCHAR,
    has_results             BOOLEAN,
    first_seen_at           TIMESTAMP NOT NULL,
    last_seen_at            TIMESTAMP NOT NULL,
    record_hash             VARCHAR NOT NULL,
    snapshot_id             VARCHAR
);

CREATE TABLE IF NOT EXISTS study_ta_evidence (
    study_id                VARCHAR NOT NULL,
    therapeutic_area        VARCHAR NOT NULL,
    matched_term            VARCHAR NOT NULL,
    matched_in              VARCHAR NOT NULL,
    PRIMARY KEY (study_id, therapeutic_area, matched_term)
);

CREATE TABLE IF NOT EXISTS study_outcome (
    outcome_uid             VARCHAR PRIMARY KEY,     -- study_id + level + ordinal
    study_id                VARCHAR NOT NULL,
    endpoint_level          VARCHAR NOT NULL,        -- primary | secondary | exploratory
    ordinal                 INTEGER NOT NULL,
    measure                 VARCHAR,
    description             VARCHAR,
    time_frame              VARCHAR,
    record_hash             VARCHAR NOT NULL,
    first_seen_at           TIMESTAMP NOT NULL,
    last_seen_at            TIMESTAMP NOT NULL
);

-- ===========================================================================
-- Layer B: study-specific endpoint specifications
-- ===========================================================================
CREATE TABLE IF NOT EXISTS endpoint_spec (
    spec_id                 VARCHAR PRIMARY KEY,
    study_id                VARCHAR NOT NULL,
    outcome_uid             VARCHAR NOT NULL,
    concept_id              VARCHAR NOT NULL,
    endpoint_level          VARCHAR NOT NULL,
    match_confidence        DOUBLE NOT NULL,
    selected_rule_id        VARCHAR,
    competing_rule_count    INTEGER NOT NULL DEFAULT 0,
    timepoint_anchor        VARCHAR,
    timepoint_selection     VARCHAR,
    timepoint_value         DOUBLE,
    timepoint_unit          VARCHAR,
    timepoint_raw           VARCHAR,
    threshold_kind          VARCHAR,
    threshold_operator      VARCHAR,
    threshold_value         DOUBLE,
    threshold_unit          VARCHAR,
    threshold_raw           VARCHAR,
    analysis_population     VARCHAR,
    direction               VARCHAR,
    scale_type              VARCHAR,
    summary_measure         VARCHAR,
    unresolved_axes         JSON,
    -- True when the winning rule tied with another on both priority and confidence,
    -- so the selection fell through to an arbitrary tie-break. Recorded rather than
    -- hidden: an arbitrary choice is exactly what a human should look at.
    ambiguous_tie           BOOLEAN NOT NULL DEFAULT FALSE,
    -- Set when a human decision replaced or amended the derived values.
    overridden              BOOLEAN NOT NULL DEFAULT FALSE,
    derivation_version      VARCHAR NOT NULL,
    classified_at           TIMESTAMP NOT NULL
);

-- Long-form structure of a spec, one row per axis, carrying where the value came from.
-- This is the table the UI reads to show, per parameter, whether a value was inherited
-- from the concept, asserted by a rule, extracted from text, or left unresolved.
CREATE TABLE IF NOT EXISTS endpoint_spec_axis (
    spec_id                 VARCHAR NOT NULL,
    axis_id                 VARCHAR NOT NULL,
    term_id                 VARCHAR NOT NULL,
    -- concept_default | rule_assert | extracted | unresolved | human_override
    origin                  VARCHAR NOT NULL,
    evidence                VARCHAR,
    PRIMARY KEY (spec_id, axis_id)
);

CREATE TABLE IF NOT EXISTS classification_evidence (
    spec_id                 VARCHAR NOT NULL,
    rulepack_id             VARCHAR NOT NULL,
    rulepack_version        VARCHAR NOT NULL,
    rule_id                 VARCHAR NOT NULL,
    concept_id              VARCHAR NOT NULL,
    priority                INTEGER NOT NULL,
    confidence              DOUBLE NOT NULL,
    selected                BOOLEAN NOT NULL,
    source_field            VARCHAR NOT NULL,
    matched_text            VARCHAR,
    span_start              INTEGER,
    span_end                INTEGER
);

CREATE TABLE IF NOT EXISTS extraction_evidence (
    spec_id                 VARCHAR NOT NULL,
    axis_id                 VARCHAR NOT NULL,
    extractor_id            VARCHAR NOT NULL,
    extractor_version       VARCHAR NOT NULL,
    source_field            VARCHAR NOT NULL,
    matched_text            VARCHAR,
    span_start              INTEGER,
    span_end                INTEGER,
    value_text              VARCHAR,
    value_num               DOUBLE,
    unit                    VARCHAR
);

-- Outcomes that no rule matched. Kept so coverage can be reported honestly and so
-- the highest-volume unmatched text is available as the queue for new rules.
CREATE TABLE IF NOT EXISTS unclassified_outcome (
    outcome_uid             VARCHAR PRIMARY KEY,
    study_id                VARCHAR NOT NULL,
    endpoint_level          VARCHAR NOT NULL,
    measure                 VARCHAR,
    normalised_measure      VARCHAR,
    derivation_version      VARCHAR NOT NULL,
    evaluated_at            TIMESTAMP NOT NULL
);

-- ===========================================================================
-- Human review
-- ===========================================================================

-- A reviewer's decision about one outcome.
--
-- Keyed by outcome_uid rather than spec_id on purpose. A spec_id is a function of
-- DERIVATION_VERSION, so keying on it would silently discard every human decision the
-- moment a rule changed -- exactly when those decisions matter most. Keying on the
-- outcome means a correction survives re-derivation and keeps being applied until the
-- underlying registry text itself changes.
--
-- The YAML under review/ is the system of record; this table is loaded from it, the
-- same relationship the vocabulary tables have with vocabularies/.
CREATE TABLE IF NOT EXISTS spec_override (
    outcome_uid             VARCHAR PRIMARY KEY,
    -- Three states, not two. A concept id names the correct concept; NULL is a real
    -- decision meaning "no concept in the vocabulary describes this", which suppresses
    -- a wrong match; the sentinel '__keep_concept__' means the reviewer corrected only
    -- axes and had no opinion about the concept. Collapsing the last two would turn an
    -- axis correction into a suppression.
    concept_id              VARCHAR,
    axes                    JSON,               -- axis_id -> term_id, applied over the derivation
    reason                  VARCHAR NOT NULL,   -- required: an unexplained override is not reviewable
    reviewer                VARCHAR NOT NULL,
    decided_at              TIMESTAMP NOT NULL,
    -- The outcome's record_hash when the decision was made. When the registry text
    -- changes the hash moves, the override is marked stale and stops being applied,
    -- because it was a judgement about text that no longer exists.
    source_hash             VARCHAR,
    supersedes_concept_id   VARCHAR,            -- what the classifier had said, for audit
    status                  VARCHAR NOT NULL DEFAULT 'active'  -- active | stale | retired
);

-- Scored evaluation runs against a gold set. Kept so accuracy is a tracked series
-- rather than a number someone once quoted in a meeting.
CREATE TABLE IF NOT EXISTS evaluation_run (
    evaluation_id           VARCHAR PRIMARY KEY,
    gold_set_id             VARCHAR NOT NULL,
    gold_set_version        VARCHAR,
    independence            VARCHAR NOT NULL,   -- self_annotated | independent | adjudicated
    derivation_version      VARCHAR NOT NULL,
    evaluated_at            TIMESTAMP NOT NULL,
    items_total             INTEGER NOT NULL,
    items_scored            INTEGER NOT NULL,
    items_stale             INTEGER NOT NULL,
    concept_accuracy        DOUBLE,
    macro_precision         DOUBLE,
    macro_recall            DOUBLE,
    macro_f1                DOUBLE,
    report                  JSON NOT NULL
);

-- ===========================================================================
-- Layer C: USDM projection
-- ===========================================================================
CREATE TABLE IF NOT EXISTS usdm_projection (
    projection_id           VARCHAR PRIMARY KEY,
    study_id                VARCHAR NOT NULL,
    usdm_version            VARCHAR NOT NULL,
    generated_at            TIMESTAMP NOT NULL,
    derivation_version      VARCHAR NOT NULL,
    endpoint_count          INTEGER NOT NULL,
    document                JSON NOT NULL
);

-- ===========================================================================
-- Pipeline control
-- ===========================================================================
CREATE TABLE IF NOT EXISTS watermark (
    source                  VARCHAR NOT NULL,
    key                     VARCHAR NOT NULL,
    value                   VARCHAR,
    updated_at              TIMESTAMP NOT NULL,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id                  VARCHAR PRIMARY KEY,
    stage                   VARCHAR NOT NULL,
    status                  VARCHAR NOT NULL,
    started_at              TIMESTAMP NOT NULL,
    finished_at             TIMESTAMP,
    stats                   JSON,
    error                   VARCHAR
);

-- ===========================================================================
-- Views
-- ===========================================================================

-- How often each concept appears, and at what level. The "where has this endpoint
-- been commonly represented" question, answered directly.
CREATE OR REPLACE VIEW concept_prevalence AS
SELECT
    c.concept_id,
    c.label,
    COUNT(*)                                                        AS spec_count,
    COUNT(DISTINCT e.study_id)                                      AS study_count,
    SUM(CASE WHEN e.endpoint_level = 'primary'     THEN 1 ELSE 0 END) AS primary_count,
    SUM(CASE WHEN e.endpoint_level = 'secondary'   THEN 1 ELSE 0 END) AS secondary_count,
    SUM(CASE WHEN e.endpoint_level = 'exploratory' THEN 1 ELSE 0 END) AS exploratory_count,
    AVG(e.match_confidence)                                         AS mean_confidence
FROM concept c
JOIN endpoint_spec e USING (concept_id)
GROUP BY c.concept_id, c.label;

-- Prevalence of each axis term across real studies, which is what tells you whether a
-- vocabulary term is earning its place or is an armchair invention.
CREATE OR REPLACE VIEW axis_term_prevalence AS
SELECT
    a.axis_id,
    a.term_id,
    t.label,
    COUNT(*)                    AS spec_count,
    COUNT(DISTINCT e.study_id)  AS study_count,
    SUM(CASE WHEN a.origin = 'extracted' THEN 1 ELSE 0 END) AS extracted_count,
    SUM(CASE WHEN a.origin = 'concept_default' THEN 1 ELSE 0 END) AS inherited_count
FROM endpoint_spec_axis a
JOIN endpoint_spec e USING (spec_id)
LEFT JOIN term t ON t.axis_id = a.axis_id AND t.term_id = a.term_id
GROUP BY a.axis_id, a.term_id, t.label;

-- What a reviewer should look at, most doubtful first.
--
-- Derived rather than stored, so it cannot drift out of step with the classification
-- it describes: re-classify and the queue is already correct. Anything a human has
-- already ruled on drops out via the anti-join, so working the queue shortens it.
--
-- The three reasons are deliberately different kinds of doubt. An arbitrary tie means
-- the engine had no principled basis for its choice. A contested match means rules
-- disagreed and one won on rank. Low confidence means the rule that fired is known to
-- be a weak signal. Unresolved axes are not doubt at all -- the source was silent --
-- so they raise the score only slightly, as a tiebreak among otherwise equal rows.
CREATE OR REPLACE VIEW review_queue AS
SELECT
    e.spec_id,
    e.outcome_uid,
    e.study_id,
    e.concept_id,
    e.endpoint_level,
    e.match_confidence,
    e.competing_rule_count,
    e.ambiguous_tie,
    o.measure,
    o.time_frame,
    json_array_length(e.unresolved_axes)                AS unresolved_count,
    CASE
        WHEN e.ambiguous_tie              THEN 'arbitrary_tie_break'
        WHEN e.competing_rule_count > 0   THEN 'contested_match'
        WHEN e.match_confidence < 0.7     THEN 'low_confidence'
        ELSE 'unresolved_parameters'
    END                                                 AS reason,
    ROUND(
        (CASE WHEN e.ambiguous_tie THEN 100 ELSE 0 END)
      + (CASE WHEN e.competing_rule_count > 0 THEN 40 ELSE 0 END)
      + (1.0 - e.match_confidence) * 50
      + json_array_length(e.unresolved_axes) * 2
    , 1)                                                AS review_score
FROM endpoint_spec e
JOIN study_outcome o USING (outcome_uid)
LEFT JOIN spec_override ov
       ON ov.outcome_uid = e.outcome_uid AND ov.status = 'active'
WHERE ov.outcome_uid IS NULL
  AND (
        e.ambiguous_tie
     OR e.competing_rule_count > 0
     OR e.match_confidence < 0.7
     OR json_array_length(e.unresolved_axes) >= 3
  );

-- Classification coverage, the honest denominator for any claim about the KB's reach.
CREATE OR REPLACE VIEW coverage_summary AS
SELECT
    o.endpoint_level,
    COUNT(*)                                            AS outcomes_total,
    COUNT(e.spec_id)                                    AS outcomes_classified,
    COUNT(*) - COUNT(e.spec_id)                         AS outcomes_unclassified,
    ROUND(100.0 * COUNT(e.spec_id) / NULLIF(COUNT(*), 0), 1) AS pct_classified
FROM study_outcome o
LEFT JOIN endpoint_spec e USING (outcome_uid)
GROUP BY o.endpoint_level;
