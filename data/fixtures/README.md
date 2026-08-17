# Fixtures

**These records are synthetic. They are not registry data and must not be cited as such.**

Every study here uses a `SYNTH-nnnn` identifier rather than an `NCT` number, so a
fixture record can never be confused with a real ClinicalTrials.gov study, and every
record carries `"_synthetic": true`. The ingest pipeline propagates that marker into
the `study.conditions` array as `__SYNTHETIC_FIXTURE__`, and the UI badges any study
carrying it.

What *is* realistic is the **phrasing** of the outcome measures. Registry outcome
titles follow strong conventions ("Change From Baseline in X at Week N", "Percentage of
Participants Achieving Y"), and these fixtures reproduce those conventions so the rule
packs and extractors are exercised against the shapes they will meet in production.
The sponsors, conditions, enrolment figures and dates are invented.

These exist because this repository was built in an environment whose egress policy
blocks `clinicaltrials.gov`. They are a test harness, not a dataset. Once the registry
host is reachable, `ceskb ingest --source ctgov` populates the same tables with real
records and the fixtures stay confined to the test suite.
