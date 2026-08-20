# Vendored USDM 4.0.0 schema

`usdm_4_0_0.json` is `components.schemas` from
[`cdisc-org/DDF-RA`](https://github.com/cdisc-org/DDF-RA)
`Deliverables/API/USDM_API.yaml` at tag **v4.0.0**, converted to a standalone
JSON Schema bundle (`$ref` targets rewritten from `#/components/schemas/` to
`#/$defs/`). Nothing else was changed.

DDF-RA is MIT for code and scripts and CC BY 4.0 for the deliverables,
© CDISC.

**Why vendored rather than imported.** `cdisc-org/usdm_api` publishes the same
model as pydantic classes, which would be the obvious dependency -- but its
`release-4-0-0` branch is GPL-3.0 (`main` was relicensed to MIT under issue
#23, after that branch was cut). Validating against DDF-RA's permissively
licensed schema keeps this repo's licensing out of play, and costs nothing:
the 4.0.0 model classes are byte-identical to 3.13.0's, so there is no
technical reason to reach for the GPL branch.

Regenerate with the snippet in `tests/test_usdm_project.py`'s docstring when
targeting a newer USDM release.
