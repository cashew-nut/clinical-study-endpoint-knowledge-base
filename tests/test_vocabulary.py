"""The vocabulary is a contract, so these tests assert its invariants rather than its
contents. A new concept should not need a test edit; a broken cross-reference should.
"""

from __future__ import annotations

import pytest

from ceskb.vocab.loader import STRUCTURE_AXES, VocabularyError, load_axes, load_concepts


def test_all_axes_load_and_are_non_empty(vocab):
    assert vocab.axes
    for axis in vocab.axes.values():
        assert axis.terms, f"axis {axis.axis_id} has no terms"


def test_concept_structure_resolves_to_real_terms(vocab):
    for concept in vocab.concepts.values():
        for key, term_id in concept.structure.items():
            axis_id, _role = STRUCTURE_AXES[key]
            assert term_id in vocab.axes[axis_id].terms, (
                f"{concept.concept_id}.{key} = {term_id!r} is not in axis {axis_id}"
            )


def test_every_concept_declares_the_five_defining_axes(vocab):
    required = {k for k, (_, role) in STRUCTURE_AXES.items() if role == "defining"}
    for concept in vocab.concepts.values():
        missing = required - set(concept.structure)
        assert not missing, f"{concept.concept_id} is missing {sorted(missing)}"


def test_rules_target_existing_concepts(vocab):
    for rule in vocab.rules:
        assert rule.concept_id in vocab.concepts, f"{rule.qualified_id} -> {rule.concept_id}"


def test_rule_regexes_compile(vocab):
    import re

    for rule in vocab.rules:
        for key in ("any_of", "all_of", "none_of"):
            for pattern in rule.match.get(key, []):
                re.compile(pattern)  # raises on a malformed pattern


def test_related_concepts_are_symmetric_where_declared(vocab):
    """A variant_of edge should not point at a concept that does not exist."""
    for concept in vocab.concepts.values():
        for relation in concept.related_concepts:
            assert relation["concept_id"] in vocab.concepts


def test_external_mappings_marked_verified_name_their_source(vocab):
    """An unsourced 'verified' claim is worse than an honest 'unverified' one."""
    for term in vocab.terms():
        for mapping in term.external_mappings:
            if mapping.get("verified"):
                assert mapping.get("verified_against"), (
                    f"{term.key} claims a verified {mapping['system']} mapping "
                    "without naming what it was checked against"
                )


def test_cdisc_endpoint_level_codes_are_the_real_ones(vocab):
    """These specific codes are load-bearing for the USDM projection."""
    expected = {"primary": "C94496", "secondary": "C139173", "exploratory": "C170559"}
    axis = vocab.axis("endpoint_level")
    for term_id, code in expected.items():
        mappings = [m for m in axis.term(term_id).external_mappings if m["system"] == "CDISC-CT"]
        assert mappings, f"{term_id} has no CDISC-CT mapping"
        assert mappings[0]["code"] == code


def test_broken_axis_reference_is_rejected(tmp_path):
    """The loader must fail loudly on a typo rather than silently dropping structure."""
    (tmp_path / "bad.yaml").write_text(
        "concepts:\n"
        "  - concept_id: BAD\n"
        "    label: Bad\n"
        "    definition: A concept with a nonexistent form.\n"
        "    therapeutic_areas: [oncology]\n"
        "    structure:\n"
        "      endpoint_form: not_a_real_form\n"
        "      measurement_concept: death_any_cause\n"
        "      reference_type: none\n"
        "      direction: longer_is_better\n"
        "      scale_type: time_to_event\n"
        "    status: draft\n"
    )
    concepts = load_concepts(tmp_path)
    axes = load_axes()
    assert "BAD" in concepts
    assert concepts["BAD"].structure["endpoint_form"] not in axes["endpoint_form"].terms


def test_axis_schema_rejects_a_malformed_term(tmp_path):
    (tmp_path / "broken.yaml").write_text(
        "axis_id: broken\nlabel: Broken\ndefinition: x\nextensible: true\n"
        "terms:\n  - term_id: Bad-ID\n    label: x\n    definition: y\n"
    )
    with pytest.raises(VocabularyError):
        load_axes(tmp_path)
