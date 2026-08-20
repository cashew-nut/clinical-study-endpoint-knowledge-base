"""The syntax-template grammar: parsing, tag classification, and rendering to
both USDM tag markup and a human label."""

from __future__ import annotations

import pytest

from clinical_endpoints.usdm.templates import (
    Literal,
    Optional,
    Tag,
    TemplateError,
    all_tags,
    parse_template,
    render,
    required_tags,
)

CFB = "Change from {reference} in {measurement}[ {timepoint}][ ({scale})]"


def test_parse_splits_literals_tags_and_optional_groups():
    parts = parse_template(CFB)
    assert parts[0] == Literal("Change from ")
    assert parts[1] == Tag("reference")
    assert isinstance(parts[-1], Optional)
    assert required_tags(parts) == ("reference", "measurement")
    assert all_tags(parts) == ("reference", "measurement", "timepoint", "scale")


def test_render_emits_usdm_tag_markup_in_text_and_values_in_label():
    parts = parse_template(CFB)
    rendered = render(
        parts,
        {"reference": "their own baseline", "measurement": "HbA1c",
         "timepoint": "at Week 24", "scale": "%"},
    )
    assert rendered.text == (
        '<p>Change from <usdm:tag name="reference"/> in <usdm:tag name="measurement"/> '
        '<usdm:tag name="timepoint"/> (<usdm:tag name="scale"/>)</p>'
    )
    assert rendered.label == "Change from their own baseline in HbA1c at Week 24 (%)"
    assert rendered.tags == ("reference", "measurement", "timepoint", "scale")
    assert rendered.dropped == ()


def test_an_optional_group_drops_whole_when_its_tag_is_unresolved():
    rendered = render(parse_template(CFB), {
        "reference": "their own baseline", "measurement": "HbA1c",
        "timepoint": None, "scale": "",
    })
    assert "timepoint" not in rendered.text and "scale" not in rendered.text
    assert rendered.label == "Change from their own baseline in HbA1c"
    assert set(rendered.dropped) == {"timepoint", "scale"}


def test_an_unresolved_required_tag_makes_the_template_not_apply():
    """The caller then drops a fidelity tier rather than emitting half a sentence."""
    assert render(parse_template(CFB), {"reference": None, "measurement": "HbA1c"}) is None


def test_literal_text_is_html_escaped_but_tag_markup_is_not():
    rendered = render(parse_template("A & B in {measurement}"), {"measurement": "X"})
    assert "A &amp; B" in rendered.text
    assert '<usdm:tag name="measurement"/>' in rendered.text
    assert rendered.label == "A & B in X"


def test_escapes_let_a_template_contain_the_delimiters():
    rendered = render(parse_template(r"\{not a tag\} {measurement}"), {"measurement": "X"})
    assert rendered.label == "{not a tag} X"


@pytest.mark.parametrize(
    "template, message",
    [
        ("Change from {reference", "unclosed {"),
        ("Change from reference}", "unmatched }"),
        ("A [B [C] D] E {m}", "nested optional group"),
        ("A [B] {m}", "optional group with no tag"),
        ("A [B {m}", "unclosed ["),
        ("A ] {m}", "unmatched ]"),
        ("{Measurement}", "invalid tag name"),
        ("", "empty template"),
        ("trailing \\", "dangling escape"),  # a lone trailing backslash, not an escaped one
    ],
)
def test_malformed_templates_are_rejected_at_parse_time(template, message):
    with pytest.raises(TemplateError, match=message.replace("[", r"\[").replace("{", r"\{")):
        parse_template(template)


def test_every_shipped_template_parses_and_only_uses_known_tags():
    """The same check `vocab validate` runs, asserted here so a bad template
    fails the unit suite too."""
    from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab
    from clinical_endpoints.vocab.schema import USDM_TAGS
    from pathlib import Path

    doc = load_vocab(default_vocab_dir(Path(__file__).parent))["usdm_templates"]
    seen = set()
    for entry in doc["templates"]:
        seen.add(entry["form"])
        if entry.get("verbatim"):
            continue
        parts = parse_template(entry["template"])
        assert required_tags(parts), f"{entry['form']} has no required tag"
        assert set(all_tags(parts)) <= USDM_TAGS, entry["form"]
    assert len(seen) == len(doc["templates"])
