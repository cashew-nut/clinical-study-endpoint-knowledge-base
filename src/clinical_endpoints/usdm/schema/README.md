# Vendored USDM 4.0.0 schema

`usdm_4_0_0.json` is `components.schemas` from
[`cdisc-org/DDF-RA`](https://github.com/cdisc-org/DDF-RA)
`Deliverables/API/USDM_API.yaml` at tag **v4.0.0**, converted to a standalone
JSON Schema bundle (`$ref` targets rewritten from `#/components/schemas/` to
`#/$defs/`). Nothing else was changed.

DDF-RA is MIT for code and scripts and CC BY 4.0 for the deliverables,
© CDISC.

Vendored rather than imported because `cdisc-org/usdm_api`'s
`release-4-0-0` branch is GPL-3.0. Regenerate with the snippet in
`tests/test_usdm_project.py` when targeting a newer USDM release.
