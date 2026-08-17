'use strict';

const main = document.getElementById('main');
const DEFINING = new Set(['endpoint_form', 'measurement_concept', 'reference_type', 'direction', 'scale_type']);

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const titleise = (s) => String(s ?? '').replace(/_/g, ' ');

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* keep statusText */ }
    throw new Error(detail);
  }
  return res.json();
}

function render(html) { main.innerHTML = html; }
function busy() { render('<div class="loading">Loading…</div>'); }

function levelBadge(level) {
  const l = level || 'exploratory';
  return `<span class="badge ${esc(l)}">${esc(l)}</span>`;
}

function syntheticBadge(isSynthetic) {
  return isSynthetic ? '<span class="badge warn" title="Synthetic fixture, not registry data">synthetic</span>' : '';
}

function bar(value, max) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return `<div class="bar"><span style="width:${pct}%"></span></div>`;
}

/** Highlight the character span a rule or extractor matched. */
function highlight(text, spans) {
  if (!text) return '<em class="empty">not stated</em>';
  const valid = (spans || [])
    .filter((s) => Number.isInteger(s.start) && Number.isInteger(s.end) && s.end > s.start && s.start < text.length)
    .sort((a, b) => a.start - b.start);
  let out = '', cursor = 0;
  for (const span of valid) {
    if (span.start < cursor) continue;         // overlapping matches: keep the first
    out += esc(text.slice(cursor, span.start));
    out += `<mark title="${esc(span.label || '')}">${esc(text.slice(span.start, span.end))}</mark>`;
    cursor = span.end;
  }
  return out + esc(text.slice(cursor));
}

function table(headers, rows, opts = {}) {
  if (!rows.length) return `<p class="empty">${esc(opts.empty || 'Nothing to show.')}</p>`;
  return `<div class="scroll"><table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td${c && c.num ? ' class="num"' : ''}>${c && c.html !== undefined ? c.html : c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// --------------------------------------------------------------------------
// views
// --------------------------------------------------------------------------
async function viewOverview() {
  busy();
  const s = await api('/api/summary');
  const c = s.counts;

  const syntheticNotice = s.synthetic_studies > 0 ? `
    <div class="notice"><strong>${s.synthetic_studies} of ${c.studies} studies are synthetic fixtures.</strong>
    They exercise the pipeline where the registry is unreachable and are badged throughout.
    Run <code>ceskb refresh --source ctgov --incremental</code> to load real registry records.</div>` : '';

  const coverage = s.coverage_by_level.map((r) => [
    levelBadge(r.endpoint_level),
    { num: true, html: r.outcomes_total },
    { num: true, html: r.outcomes_classified },
    { num: true, html: r.outcomes_unclassified },
    `${bar(r.pct_classified, 100)}<span class="mono">${r.pct_classified}%</span>`,
  ]);

  const runs = s.recent_runs.map((r) => [
    esc(r.stage),
    r.status === 'succeeded' ? '<span class="badge primary">ok</span>' : `<span class="badge warn">${esc(r.status)}</span>`,
    `<span class="mono">${esc(String(r.started_at).slice(0, 19))}</span>`,
    `<code>${esc(JSON.stringify(r.stats).slice(0, 110))}</code>`,
  ]);

  render(`
    ${syntheticNotice}
    <div class="card">
      <h2>Knowledge base</h2>
      <p class="hint">Layer A is authored in git and rebuilt into the database on every run; Layers B and C are derived.</p>
      <div class="grid cols-4">
        <div class="stat"><div class="n">${c.concepts}</div><div class="k">Concepts</div></div>
        <div class="stat"><div class="n">${c.axes}</div><div class="k">Axes</div></div>
        <div class="stat"><div class="n">${c.terms}</div><div class="k">Vocabulary terms</div></div>
        <div class="stat"><div class="n">${c.rules}</div><div class="k">Classification rules</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Connected study data</h2>
      <p class="hint">Registry outcomes mapped onto canonical concepts, and projected to USDM ${esc(s.usdm_version)}.</p>
      <div class="grid cols-4">
        <div class="stat"><div class="n">${c.studies}</div><div class="k">Studies</div></div>
        <div class="stat"><div class="n">${c.outcomes}</div><div class="k">Registry outcomes</div></div>
        <div class="stat"><div class="n">${c.specs}</div><div class="k">Endpoint specs</div></div>
        <div class="stat"><div class="n">${s.coverage_pct}%</div><div class="k">Classified</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Coverage by endpoint level</h2>
      <p class="hint">Unclassified outcomes are counted, not hidden. They are the queue for new rules.</p>
      ${table(['Level', 'Outcomes', 'Classified', 'Unclassified', 'Coverage'], coverage)}
    </div>

    <div class="card">
      <h2>Recent pipeline runs</h2>
      ${table(['Stage', 'Status', 'Started', 'Stats'], runs, { empty: 'No runs recorded.' })}
    </div>`);
}

let conceptFilters = { q: '', ta: '', form: '', observed: false };

async function viewConcepts() {
  busy();
  const params = new URLSearchParams();
  if (conceptFilters.q) params.set('q', conceptFilters.q);
  if (conceptFilters.ta) params.set('therapeutic_area', conceptFilters.ta);
  if (conceptFilters.form) params.set('endpoint_form', conceptFilters.form);
  if (conceptFilters.observed) params.set('only_observed', 'true');

  const [concepts, axes] = await Promise.all([
    api('/api/concepts?' + params.toString()),
    api('/api/axes'),
  ]);
  const forms = await api('/api/axes/endpoint_form');
  const tas = await api('/api/axes/therapeutic_area');
  const maxSpecs = Math.max(1, ...concepts.map((c) => c.spec_count));

  const rows = concepts.map((c) => [
    `<button class="link" onclick="go('/concept/${esc(c.concept_id)}')"><strong>${esc(c.label)}</strong></button>
     <div class="mono" style="color:var(--ink-3)">${esc(c.concept_id)}</div>`,
    `<span class="badge neutral">${esc(titleise(c.endpoint_form))}</span>`,
    `<span class="mono">${esc(titleise(c.measurement_concept))}</span>`,
    `<div class="tag-list">${c.therapeutic_areas.map((t) => `<span class="badge neutral">${esc(titleise(t))}</span>`).join('')}</div>`,
    { num: true, html: `${bar(c.spec_count, maxSpecs)}<span class="mono">${c.spec_count}</span>` },
    { num: true, html: c.study_count },
    { num: true, html: c.primary_count },
  ]);

  render(`
    <div class="card">
      <h2>Canonical endpoint concepts</h2>
      <p class="hint">Layer A. Each concept carries reusable clinical meaning independent of any study. Sorted by how often it is observed.</p>
      <div class="controls">
        <input type="search" id="q" placeholder="Search concepts…" value="${esc(conceptFilters.q)}">
        <select id="ta"><option value="">All therapeutic areas</option>
          ${tas.terms.map((t) => `<option value="${esc(t.term_id)}"${conceptFilters.ta === t.term_id ? ' selected' : ''}>${esc(t.label)}</option>`).join('')}
        </select>
        <select id="form"><option value="">All forms</option>
          ${forms.terms.map((t) => `<option value="${esc(t.term_id)}"${conceptFilters.form === t.term_id ? ' selected' : ''}>${esc(t.label)}</option>`).join('')}
        </select>
        <label class="check"><input type="checkbox" id="observed"${conceptFilters.observed ? ' checked' : ''}> Observed in study data only</label>
      </div>
      ${table(['Concept', 'Form', 'Measurement', 'Therapeutic areas', 'Specs', 'Studies', 'Primary'], rows,
        { empty: 'No concepts match these filters.' })}
    </div>`);

  const q = document.getElementById('q');
  q.oninput = debounce(() => { conceptFilters.q = q.value; viewConcepts(); }, 250);
  document.getElementById('ta').onchange = (e) => { conceptFilters.ta = e.target.value; viewConcepts(); };
  document.getElementById('form').onchange = (e) => { conceptFilters.form = e.target.value; viewConcepts(); };
  document.getElementById('observed').onchange = (e) => { conceptFilters.observed = e.target.checked; viewConcepts(); };
}

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function viewConcept(id) {
  busy();
  const d = await api(`/api/concepts/${encodeURIComponent(id)}`);
  const c = d.concept;

  const axisCells = d.structure.map((s) => `
    <div class="axis-cell ${DEFINING.has(s.axis_id) ? 'defining' : ''}">
      <div class="ax">${esc(s.axis_label || s.axis_id)}${s.role === 'default' ? ' · default' : ''}</div>
      <div class="tm">${esc(s.term_label || s.term_id)}</div>
      <div class="df">${esc((s.term_definition || '').slice(0, 155))}</div>
    </div>`).join('');

  const th = d.definitional_threshold;
  const thresholdBlock = th ? `
    <div class="card">
      <h2>Definitional threshold</h2>
      <p class="hint">Part of what the concept means. Thresholds a protocol chooses live in Layer B instead.</p>
      <dl class="kv">
        <dt>Kind</dt><dd>${esc(titleise(th.kind))}</dd>
        <dt>Rule</dt><dd><code>${esc(titleise(th.operator))} ${esc(th.value_text)}${th.unit ? ' ' + esc(titleise(th.unit)) : ''}</code></dd>
        ${th.applies_to ? `<dt>Applies to</dt><dd>${esc(th.applies_to)}</dd>` : ''}
        ${th.note ? `<dt>Note</dt><dd>${esc(th.note)}</dd>` : ''}
      </dl>
    </div>` : '';

  const evidenceRows = d.evidence.map((e) => [
    `<button class="link mono" onclick="go('/study/${esc(e.study_id)}')">${esc(e.study_id)}</button> ${syntheticBadge(e.is_synthetic)}
     <div style="color:var(--ink-3);font-size:12.5px">${esc((e.brief_title || '').slice(0, 70))}</div>`,
    levelBadge(e.endpoint_level),
    `<div style="max-width:380px">${esc(e.measure)}</div>`,
    e.timepoint_value != null ? `<span class="mono">${e.timepoint_value} ${esc(e.timepoint_unit || '')}</span>` : '<span style="color:var(--ink-3)">—</span>',
    `<span class="mono">${esc(titleise(e.analysis_population || '—'))}</span>`,
    `<button class="link" onclick="go('/spec/${esc(e.spec_id)}')">trace</button>`,
  ]);

  const observedByAxis = {};
  for (const o of d.observed_parameters) (observedByAxis[o.axis_id] ||= []).push(o);
  const observedBlock = Object.keys(observedByAxis).length ? `
    <div class="card">
      <h2>Parameter values observed in study data</h2>
      <p class="hint">What protocols actually chose, as opposed to what the concept defaults to. Divergence here is the signal that a default needs revisiting.</p>
      ${table(['Axis', 'Value', 'Origin', 'Count'], Object.entries(observedByAxis).flatMap(([axis, vals]) =>
        vals.map((v, i) => [
          i === 0 ? `<strong>${esc(titleise(axis))}</strong>` : '',
          esc(titleise(v.term_id)),
          `<span class="origin ${esc(v.origin)}">${esc(titleise(v.origin))}</span>`,
          { num: true, html: v.n },
        ])))}
    </div>` : '';

  const rulesRows = d.rules.map((r) => [
    `<code>${esc(r.rulepack_id)}@${esc(r.rulepack_version)}#${esc(r.rule_id)}</code>`,
    { num: true, html: r.priority },
    { num: true, html: r.confidence },
    `<code>${esc(JSON.stringify(r.match_spec).slice(0, 160))}</code>`,
    Object.keys(r.asserts || {}).length ? `<code>${esc(JSON.stringify(r.asserts))}</code>` : '<span style="color:var(--ink-3)">—</span>',
  ]);

  render(`
    <div class="crumb"><button class="link" onclick="go('/concepts')">Concepts</button> / ${esc(c.concept_id)}</div>

    <div class="card">
      <h2>${esc(c.label)}${c.abbreviation ? ` <span class="badge accent">${esc(c.abbreviation)}</span>` : ''}</h2>
      <p class="hint mono">${esc(c.concept_id)} · status ${esc(c.status)} · defined in ${esc(c.source_file)}</p>
      <p>${esc(c.definition)}</p>
      ${c.synonyms.length ? `<div class="tag-list" style="margin-top:10px">${c.synonyms.map((s) => `<span class="badge neutral">${esc(s)}</span>`).join('')}</div>` : ''}
      ${c.notes ? `<p style="margin-top:12px;color:var(--ink-2);font-size:14px"><strong>Note.</strong> ${esc(c.notes)}</p>` : ''}
    </div>

    <div class="card">
      <h2>Endpoint structure</h2>
      <p class="hint">Blue-edged axes are defining: change one and it is a different endpoint. The rest are defaults a protocol may override.</p>
      <div class="axes">${axisCells}</div>
    </div>

    ${thresholdBlock}

    ${d.components.length ? `<div class="card"><h2>Components</h2>
      ${table(['#', 'Component', 'Measurement', 'Note'], d.components.map((x) => [
        { num: true, html: x.ordinal + 1 }, esc(x.label),
        `<span class="mono">${esc(titleise(x.measurement_concept || '—'))}</span>`,
        esc(x.note || '')]))}</div>` : ''}

    ${d.governing_criteria.length ? `<div class="card"><h2>Governing criteria</h2>
      ${table(['Criteria', 'Version', 'Citation'], d.governing_criteria.map((x) => [
        esc(x.name), esc(x.version || '—'), esc(x.citation || '')]))}</div>` : ''}

    ${d.relations.length ? `<div class="card"><h2>Related concepts</h2>
      ${table(['Relation', 'Concept', 'Note'], d.relations.map((r) => [
        `<span class="badge neutral">${esc(titleise(r.relation))}</span>`,
        `<button class="link" onclick="go('/concept/${esc(r.related_concept_id)}')">${esc(r.label || r.related_concept_id)}</button>`,
        esc(r.note || '')]))}</div>` : ''}

    <div class="card">
      <h2>Where this endpoint is represented</h2>
      <p class="hint">${d.evidence.length} specification${d.evidence.length === 1 ? '' : 's'} across the ingested studies. Follow any trace to see exactly why it was classified this way.</p>
      ${table(['Study', 'Level', 'Registry outcome text', 'Timepoint', 'Population', ''], evidenceRows,
        { empty: 'Not yet observed in the ingested studies.' })}
    </div>

    ${observedBlock}

    <div class="card">
      <h2>Rules that map onto this concept</h2>
      <p class="hint">The complete classification logic. Nothing else assigns this concept.</p>
      ${table(['Rule', 'Priority', 'Confidence', 'Match', 'Asserts'], rulesRows, { empty: 'No rules target this concept yet.' })}
    </div>`);
}

async function viewVocabulary() {
  busy();
  const axes = await api('/api/axes');
  const rows = axes.map((a) => [
    `<button class="link" onclick="go('/axis/${esc(a.axis_id)}')"><strong>${esc(a.label)}</strong></button>
     <div class="mono" style="color:var(--ink-3)">${esc(a.axis_id)}</div>`,
    { num: true, html: a.term_count },
    a.extensible ? '<span class="badge neutral">extensible</span>' : '<span class="badge accent">closed</span>',
    a.usdm_codelist_c_code
      ? `<span class="mono">${esc(a.usdm_entity)}.${esc(a.usdm_attribute)} · ${esc(a.usdm_codelist_c_code)}</span>`
      : '<span style="color:var(--ink-3)">—</span>',
    `<div style="max-width:520px;color:var(--ink-2);font-size:13.5px">${esc(a.definition)}</div>`,
  ]);
  render(`<div class="card">
      <h2>Controlled vocabulary axes</h2>
      <p class="hint">Each axis is one dimension of endpoint structure. Axes aligned to a USDM attribute carry the CDISC codelist they map to.</p>
      ${table(['Axis', 'Terms', 'Extensibility', 'USDM alignment', 'Definition'], rows)}
    </div>`);
}

async function viewAxis(id) {
  busy();
  const d = await api(`/api/axes/${encodeURIComponent(id)}`);
  const maxSpecs = Math.max(1, ...d.terms.map((t) => t.spec_count));
  const rows = d.terms.map((t) => [
    `<strong>${esc(t.label)}</strong><div class="mono" style="color:var(--ink-3)">${esc(t.term_id)}</div>
     ${t.broader ? `<div style="font-size:12px;color:var(--ink-3)">narrower than <code>${esc(t.broader)}</code></div>` : ''}`,
    `<div style="max-width:430px;color:var(--ink-2);font-size:13.5px">${esc(t.definition)}</div>
     ${t.synonyms.length ? `<div class="tag-list" style="margin-top:5px">${t.synonyms.slice(0, 6).map((s) => `<span class="badge neutral">${esc(s)}</span>`).join('')}</div>` : ''}`,
    t.external_mappings.length
      ? t.external_mappings.map((m) => `<div class="mono">${esc(m.system)} ${esc(m.code)}
          ${m.verified ? '<span class="badge primary">verified</span>' : '<span class="badge warn">unverified</span>'}</div>`).join('')
      : '<span style="color:var(--ink-3)">—</span>',
    { num: true, html: `${bar(t.spec_count, maxSpecs)}<span class="mono">${t.spec_count}</span>` },
  ]);
  render(`
    <div class="crumb"><button class="link" onclick="go('/vocabulary')">Vocabulary</button> / ${esc(d.axis.axis_id)}</div>
    <div class="card">
      <h2>${esc(d.axis.label)}</h2>
      <p class="hint">${esc(d.axis.definition)}</p>
      ${table(['Term', 'Definition', 'External mappings', 'Observed'], rows)}
    </div>`);
}

let studyQuery = '';

async function viewStudies() {
  busy();
  const studies = await api('/api/studies?limit=500' + (studyQuery ? `&q=${encodeURIComponent(studyQuery)}` : ''));
  const rows = studies.map((s) => [
    `<button class="link mono" onclick="go('/study/${esc(s.study_id)}')">${esc(s.study_id)}</button> ${syntheticBadge(s.is_synthetic)}`,
    `<div style="max-width:390px">${esc(s.brief_title || '')}</div>`,
    `<div class="tag-list">${s.therapeutic_areas.map((t) => `<span class="badge neutral">${esc(titleise(t))}</span>`).join('') || '<span style="color:var(--ink-3)">—</span>'}</div>`,
    esc((s.phases || []).join(', ') || '—'),
    { num: true, html: s.spec_count },
  ]);
  render(`<div class="card">
      <h2>Studies</h2>
      <p class="hint">Registry records ingested into Layer B.</p>
      <div class="controls"><input type="search" id="sq" placeholder="Search studies…" value="${esc(studyQuery)}"></div>
      ${table(['Study', 'Title', 'Therapeutic areas', 'Phase', 'Endpoints'], rows, { empty: 'No studies match.' })}
    </div>`);
  const sq = document.getElementById('sq');
  sq.oninput = debounce(() => { studyQuery = sq.value; viewStudies(); }, 250);
}

async function viewStudy(id) {
  busy();
  const d = await api(`/api/studies/${encodeURIComponent(id)}`);
  const s = d.study;
  const rows = d.outcomes.map((o) => [
    levelBadge(o.endpoint_level),
    `<div style="max-width:440px">${esc(o.measure || '')}</div>
     ${o.time_frame ? `<div style="font-size:12.5px;color:var(--ink-3)">${esc(o.time_frame)}</div>` : ''}`,
    o.concept_id
      ? `<button class="link" onclick="go('/concept/${esc(o.concept_id)}')">${esc(o.concept_label || o.concept_id)}</button>`
      : '<span class="badge warn">unclassified</span>',
    o.match_confidence != null ? `<span class="mono">${o.match_confidence}</span>` : '—',
    o.spec_id ? `<button class="link" onclick="go('/spec/${esc(o.spec_id)}')">trace</button>` : '',
  ]);
  render(`
    <div class="crumb"><button class="link" onclick="go('/studies')">Studies</button> / ${esc(s.study_id)}</div>
    ${s.is_synthetic ? '<div class="notice"><strong>Synthetic fixture.</strong> This record is not registry data.</div>' : ''}
    <div class="card">
      <h2>${esc(s.brief_title || s.study_id)}</h2>
      <p class="hint mono">${esc(s.study_id)} · ${esc(s.source)}</p>
      <dl class="kv">
        <dt>Official title</dt><dd>${esc(s.official_title || '—')}</dd>
        <dt>Sponsor</dt><dd>${esc(s.lead_sponsor || '—')}</dd>
        <dt>Status</dt><dd>${esc(s.overall_status || '—')}</dd>
        <dt>Phase</dt><dd>${esc((s.phases || []).join(', ') || '—')}</dd>
        <dt>Conditions</dt><dd>${esc((s.conditions || []).filter((c) => c !== '__SYNTHETIC_FIXTURE__').join('; '))}</dd>
        <dt>Therapeutic areas</dt><dd>${esc((s.therapeutic_areas || []).map(titleise).join(', ') || '—')}</dd>
        <dt>Last updated</dt><dd>${esc(s.last_update_posted || '—')}</dd>
      </dl>
    </div>
    <div class="card">
      <h2>Outcomes</h2>
      ${table(['Level', 'Registry text', 'Canonical concept', 'Confidence', ''], rows)}
    </div>
    ${d.has_usdm ? `<div class="card"><h2>Layer C</h2>
      <p class="hint">USDM projection for this study.</p>
      <button class="link" onclick="go('/usdm/${esc(s.study_id)}')">View USDM document →</button></div>` : ''}`);
}

async function viewSpec(id) {
  busy();
  const d = await api(`/api/specs/${encodeURIComponent(id)}`);
  const s = d.spec;

  const spansFor = (field) => [
    ...d.rule_evidence.filter((r) => r.selected && (r.source_field === field ||
        (r.source_field === 'measure_and_description' && field === 'measure')))
      .map((r) => ({ start: r.span_start, end: r.span_end, label: `rule ${r.rule_id}` })),
    ...d.extraction_evidence.filter((e) => e.source_field === field)
      .map((e) => ({ start: e.span_start, end: e.span_end, label: `${e.axis_id} → ${e.extractor_id}` })),
  ];

  const axisRows = d.axes.map((a) => [
    `<strong>${esc(a.axis_label || a.axis_id)}</strong><div class="mono" style="color:var(--ink-3)">${esc(a.axis_id)}</div>`,
    esc(a.term_label || titleise(a.term_id)),
    `<span class="origin ${esc(a.origin)}">${esc(titleise(a.origin))}</span>`,
    a.evidence ? `<code>${esc(a.evidence)}</code>` : '<span style="color:var(--ink-3)">—</span>',
  ]);

  const ruleRows = d.rule_evidence.map((r) => [
    r.selected ? '<span class="badge primary">selected</span>' : '<span class="badge neutral">competing</span>',
    `<code>${esc(r.rulepack_id)}#${esc(r.rule_id)}</code>`,
    `<button class="link" onclick="go('/concept/${esc(r.concept_id)}')">${esc(r.concept_id)}</button>`,
    { num: true, html: r.priority },
    { num: true, html: r.confidence },
    `<code>${esc(r.matched_text || '')}</code>`,
  ]);

  const extractionRows = d.extraction_evidence.map((e) => [
    `<span class="mono">${esc(e.axis_id)}</span>`,
    `<code>${esc(e.extractor_id)}@${esc(e.extractor_version)}</code>`,
    esc(e.source_field),
    `<code>${esc(e.matched_text || '')}</code>`,
    e.value_num != null ? `<span class="mono">${e.value_num}${e.unit ? ' ' + esc(e.unit) : ''}</span>` : '—',
  ]);

  render(`
    <div class="crumb">
      <button class="link" onclick="go('/studies')">Studies</button> /
      <button class="link" onclick="go('/study/${esc(s.study_id)}')">${esc(s.study_id)}</button> / trace
    </div>
    ${s.is_synthetic ? '<div class="notice"><strong>Synthetic fixture.</strong> This record is not registry data.</div>' : ''}

    <div class="card">
      <h2>${esc(s.concept_id)} ${levelBadge(s.endpoint_level)}
        ${s.overridden ? '<span class="badge exploratory">reviewed</span>' : ''}
        ${s.ambiguous_tie ? '<span class="badge warn">arbitrary tie-break</span>' : ''}</h2>
      <p class="hint">Derivation ${esc(s.derivation_version)} · rule <code>${esc(s.selected_rule_id || '—')}</code> ·
        confidence ${s.match_confidence} · ${s.competing_rule_count} competing rule${s.competing_rule_count === 1 ? '' : 's'}</p>
      ${s.ambiguous_tie ? `<div class="notice"><strong>The winning rule tied.</strong> Another rule
        naming a different concept matched at the same priority and the same confidence, so the
        choice between them fell through to an alphabetical tie-break — a way to stay
        deterministic, not a way to be right.
        <button class="link" onclick="go('/review')">Review this</button>.</div>` : ''}
      ${s.overridden ? `<div class="notice info"><strong>Carries a reviewer's decision.</strong> Values
        marked <span class="origin human_override">human override</span> below were set by a person,
        not derived. <button class="link" onclick="go('/review')">See the decision</button>.</div>` : ''}
    </div>

    <div class="card">
      <h2>Source text</h2>
      <p class="hint">Highlights are the exact spans that fired the selected rule and the extractors.</p>
      <div class="src-text"><span class="lbl">Measure</span>${highlight(s.measure, spansFor('measure'))}</div>
      <div class="src-text"><span class="lbl">Description</span>${highlight(s.description, spansFor('description'))}</div>
      <div class="src-text"><span class="lbl">Time frame</span>${highlight(s.time_frame, spansFor('time_frame'))}</div>
    </div>

    <div class="card">
      <h2>Resolved parameters</h2>
      <p class="hint">Every axis with the origin of its value. <span class="origin concept_default">Concept default</span> is inherited from Layer A,
        <span class="origin rule_assert">rule assert</span> is a rule overriding it, <span class="origin extracted">extracted</span> comes from this study's text,
        <span class="origin unresolved">unresolved</span> means the source did not say, and
        <span class="origin human_override">human override</span> is a reviewer's decision, which beats all of them.</p>
      ${table(['Axis', 'Value', 'Origin', 'Evidence'], axisRows)}
      ${s.unresolved_axes.length ? `<p style="margin-top:12px;font-size:13.5px;color:var(--ink-3)">
        Unresolved: ${s.unresolved_axes.map((a) => `<code>${esc(a)}</code>`).join(', ')}</p>` : ''}
    </div>

    <div class="card">
      <h2>Rule evidence</h2>
      <p class="hint">Every rule that fired, including the ones that lost, so a disputed classification can be argued from the record.</p>
      ${table(['', 'Rule', 'Concept', 'Priority', 'Confidence', 'Matched'], ruleRows)}
    </div>

    <div class="card">
      <h2>Extraction evidence</h2>
      ${table(['Axis', 'Extractor', 'Field', 'Matched', 'Value'], extractionRows, { empty: 'No extractions.' })}
    </div>`);
}

async function viewUsdm(id) {
  busy();
  const doc = await api(`/api/usdm/${encodeURIComponent(id)}`);
  const design = doc.study.versions[0].studyDesigns[0];
  const endpoints = design.objectives.flatMap((o) => o.endpoints.map((e) => ({ o, e })));
  const dictById = Object.fromEntries(design.dictionaries.map((d) => [d.id, d]));

  const rows = endpoints.map(({ o, e }) => {
    const dict = dictById[e.dictionaryId];
    const maps = (dict?.parameterMaps || [])
      .map((p) => `<div class="mono" style="font-size:11.5px">[${esc(p.tag)}] → ${esc(p.reference.slice(0, 78))}</div>`).join('');
    return [
      `<span class="badge ${esc(e.level.decode.split(' ')[0].toLowerCase())}">${esc(e.level.decode)}</span>
       <div class="mono" style="color:var(--ink-3);font-size:11.5px">${esc(e.level.code)}</div>`,
      `<div style="max-width:430px">${esc(e.text)}</div>`,
      maps || '<span style="color:var(--ink-3)">—</span>',
    ];
  });

  render(`
    <div class="crumb">
      <button class="link" onclick="go('/study/${esc(id)}')">${esc(id)}</button> / USDM
    </div>
    <div class="card">
      <h2>USDM ${esc(doc.usdmVersion)} projection</h2>
      <p class="hint">${doc._provenance.endpoint_count} endpoints · generated ${esc(doc._provenance.generated)} · ${esc(doc._provenance.derivation_version)}</p>
      <p style="font-size:13.5px;color:var(--ink-2)">${esc(doc._provenance.caveat)}</p>
      ${doc._provenance.unresolved.length ? `<p style="font-size:13.5px;color:var(--ink-3)">Unresolved axes across this study:
        ${doc._provenance.unresolved.map((a) => `<code>${esc(a)}</code>`).join(', ')}</p>` : ''}
    </div>
    <div class="card">
      <h2>Endpoints and parameter maps</h2>
      <p class="hint">Each endpoint is a SyntaxTemplate whose <code>[Tags]</code> resolve through a SyntaxTemplateDictionary to real objects.</p>
      ${table(['Level (NCI code)', 'Endpoint text', 'Parameter maps'], rows)}
    </div>
    <div class="card">
      <h2>Full document</h2>
      <pre>${esc(JSON.stringify(doc, null, 2))}</pre>
    </div>`);
}

async function viewGaps() {
  busy();
  const [unclassified, prevalence] = await Promise.all([
    api('/api/unclassified?limit=200'),
    api('/api/prevalence/axes'),
  ]);
  const unusedTerms = prevalence.filter((p) => p.spec_count === 0);

  render(`
    <div class="card">
      <h2>Unclassified outcome text</h2>
      <p class="hint">Registry outcomes no rule matched, most frequent first. This is the work queue: each row is either a missing rule or a missing concept.</p>
      ${table(['Outcome text', 'Count', 'Example study', 'Level'], unclassified.map((u) => [
        `<div style="max-width:560px">${esc(u.normalised_measure)}</div>`,
        { num: true, html: u.n },
        `<button class="link mono" onclick="go('/study/${esc(u.example_study)}')">${esc(u.example_study)}</button>`,
        levelBadge(u.level),
      ]), { empty: 'Every ingested outcome was classified.' })}
    </div>

    <div class="card">
      <h2>Vocabulary terms not yet observed</h2>
      <p class="hint">Terms defined in Layer A that no ingested study has exercised. Some are genuinely rare; others are a sign the vocabulary got ahead of the evidence.</p>
      ${table(['Axis', 'Term'], unusedTerms.map((t) => [
        `<button class="link mono" onclick="go('/axis/${esc(t.axis_id)}')">${esc(t.axis_id)}</button>`,
        esc(t.label || t.term_id),
      ]), { empty: 'Every vocabulary term has been observed.' })}
    </div>`);
}

// --------------------------------------------------------------------------
// review
// --------------------------------------------------------------------------
const REASON_BLURB = {
  arbitrary_tie_break: 'Two rules for different concepts tied on both priority and confidence, so the winner was picked alphabetically. There was no principled basis for the choice.',
  contested_match: 'More than one rule fired and the winner was chosen on rank.',
  low_confidence: 'The rule that fired is known to be a weak signal.',
  unresolved_parameters: 'The source text left several parameters unstated.',
};

async function viewReview() {
  busy();
  const [queue, overrides, evaluations, health] = await Promise.all([
    api('/api/review/queue?limit=200'),
    api('/api/review/overrides'),
    api('/api/review/evaluations'),
    api('/healthz'),
  ]);
  const writable = health.review_writes === 'true';
  const latest = evaluations[0];

  const accuracyCard = latest ? (() => {
    const r = latest.report || {};
    const selfAnnotated = latest.independence === 'self_annotated';
    const metric = (k, v, bad) =>
      `<div class="metric${bad ? ' bad' : ''}"><div class="v">${v}</div><div class="k">${esc(k)}</div></div>`;
    return `
    <div class="card">
      <h2>Accuracy against the gold set</h2>
      <p class="hint">Scored ${latest.items_scored} of ${latest.items_total} annotations from
        <code>${esc(latest.gold_set_id)}</code> under derivation ${esc(latest.derivation_version)}.</p>
      <div class="metric-row">
        ${metric('concept accuracy', (latest.concept_accuracy * 100).toFixed(1) + '%')}
        ${metric('macro precision', latest.macro_precision.toFixed(3))}
        ${metric('macro recall', latest.macro_recall.toFixed(3))}
        ${metric('excluded', latest.items_stale, latest.items_stale > 0)}
      </div>
      ${selfAnnotated ? `<div class="notice" style="margin-top:14px"><strong>Self-annotated.</strong>
        ${esc(r.caveat || '')}</div>` : ''}
      ${(r.errors || []).length ? `<h2 style="margin-top:18px">Disagreements</h2>
        ${table(['Outcome', 'Gold', 'Predicted', 'Text'], r.errors.map((e) => [
          `<span class="mono">${esc(e.outcome_uid)}</span>`,
          `<code>${esc(e.gold)}</code>`,
          `<code>${esc(e.predicted)}</code>`,
          `<div style="max-width:420px">${esc(e.text)}</div>`,
        ]))}` : ''}
    </div>`;
  })() : `
    <div class="card">
      <h2>Accuracy against the gold set</h2>
      <p class="hint">No evaluation has been run. Coverage is not accuracy: until annotations
        are scored, nothing here reports whether a classification is <em>right</em>, only that
        one was made. Run <code>ceskb evaluate</code>.</p>
    </div>`;

  const queueRows = queue.queue.map((q) => [
    `<span class="reason-chip ${esc(q.reason)}" title="${esc(REASON_BLURB[q.reason] || '')}">${esc(titleise(q.reason))}</span>`,
    `<div style="max-width:400px">${esc(q.measure)}</div>
     <div class="mono" style="color:var(--ink-3);font-size:11.5px">${esc(q.outcome_uid)}</div>`,
    `<button class="link" onclick="go('/concept/${esc(q.concept_id)}')">${esc(q.concept_id)}</button>`,
    { num: true, html: q.match_confidence.toFixed(2) },
    { num: true, html: `<span class="score">${q.review_score}</span>` },
    `<button class="link" onclick="go('/spec/${esc(q.spec_id)}')">trace</button>`,
  ]);

  const overrideRows = overrides.map((o) => [
    o.status === 'active' ? '<span class="badge primary">active</span>'
      : `<span class="badge warn">${esc(o.status)}</span>`,
    `<div style="max-width:360px">${esc(o.measure || '—')}</div>
     <div class="mono" style="color:var(--ink-3);font-size:11.5px">${esc(o.outcome_uid)}</div>`,
    o.concept_id === null ? '<span class="badge warn">no concept fits</span>'
      : o.concept_id === '__keep_concept__' ? '<span style="color:var(--ink-3)">axes only</span>'
      : `<code>${esc(o.concept_id)}</code>`,
    Object.keys(o.axes || {}).length
      ? `<div class="mono" style="font-size:11.5px">${Object.entries(o.axes)
          .map(([k, v]) => `${esc(k)}=${esc(v)}`).join('<br>')}</div>`
      : '—',
    `<div style="max-width:340px">${esc(o.reason)}</div>`,
    esc(o.reviewer),
  ]);

  render(`
    ${accuracyCard}

    <div class="card">
      <h2>Review queue</h2>
      <p class="hint">Specifications with a reason to doubt them, most doubtful first. Working the
        queue shortens it: anything decided drops out. Hover a reason to see what it means.</p>
      <div class="tag-list" style="margin-bottom:12px">
        ${queue.by_reason.map((r) =>
          `<span class="reason-chip ${esc(r.reason)}">${esc(titleise(r.reason))} · ${r.n}</span>`).join('')
          || '<span style="color:var(--ink-3);font-size:13px">Nothing queued.</span>'}
      </div>
      ${table(['Reason', 'Outcome', 'Concept', 'Confidence', 'Score', ''], queueRows,
        { empty: 'Nothing is awaiting review.' })}
    </div>

    <div class="card">
      <h2>Record a decision</h2>
      ${writable
        ? `<p class="hint">Written to <code>review/overrides.yaml</code> and applied immediately.
             Decisions are keyed by outcome, so they survive a rule change; they go stale by
             themselves if the registry rewrites the text underneath them.</p>
           <div class="decide">
             <div class="wide">
               <label for="d-uid">Outcome</label>
               <input id="d-uid" placeholder="e.g. SYNTH-0001:primary:0" list="d-uids">
               <datalist id="d-uids">${queue.queue.map((q) =>
                 `<option value="${esc(q.outcome_uid)}">`).join('')}</datalist>
             </div>
             <div>
               <label for="d-concept">Concept</label>
               <input id="d-concept" placeholder="ORR — or leave blank">
             </div>
             <div>
               <label for="d-axis">Axis correction</label>
               <input id="d-axis" placeholder="axis_id=term_id">
             </div>
             <div>
               <label for="d-reviewer">Reviewer</label>
               <input id="d-reviewer" placeholder="your name">
             </div>
             <div class="wide">
               <label for="d-reason">Reason (required, and it is stored)</label>
               <input id="d-reason" placeholder="What you checked, and what it says">
             </div>
             <div>
               <button class="btn" id="d-save">Record decision</button>
             </div>
             <div>
               <button class="btn ghost" id="d-none">Record “no concept fits”</button>
             </div>
           </div>
           <div id="d-result" style="margin-top:12px"></div>`
        : `<p class="hint">The API is read-only. Restart with <code>ceskb serve --allow-review</code>
             to record decisions here, or use the command line:</p>
           <pre class="code">ceskb override OUTCOME_UID --concept ORR \\
    --reason "Checked against the protocol: best overall response of CR or PR." \\
    --reviewer "your name"</pre>`}
    </div>

    <div class="card">
      <h2>Decisions on record</h2>
      <p class="hint">Every reviewer decision, including those that have gone stale because the
        source text changed. Stale decisions stop being applied and wait for re-review, because
        they were judgements about words that no longer exist.</p>
      ${table(['', 'Outcome', 'Concept', 'Axes', 'Reason', 'Reviewer'], overrideRows,
        { empty: 'No decisions recorded yet.' })}
    </div>`);

  if (!writable) return;

  const val = (id) => document.getElementById(id).value.trim();
  const submit = async (suppress) => {
    const out = document.getElementById('d-result');
    const body = { outcome_uid: val('d-uid'), reason: val('d-reason'), reviewer: val('d-reviewer') };
    if (suppress) body.concept_id = null;
    else if (val('d-concept')) body.concept_id = val('d-concept');
    const axis = val('d-axis');
    if (axis.includes('=')) {
      const [k, v] = axis.split('=');
      body.axes = { [k.trim()]: v.trim() };
    }
    out.innerHTML = '<span class="hint">Recording…</span>';
    try {
      const res = await fetch('/api/review/overrides', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      out.innerHTML = `<div class="notice"><strong>Recorded.</strong> Re-derived
        ${data.reclassified.classified} specification(s) from ${data.reclassified.outcomes} outcomes.</div>`;
      setTimeout(() => viewReview(), 700);
    } catch (err) {
      out.innerHTML = `<div class="notice"><strong>Rejected.</strong> ${esc(err.message)}</div>`;
    }
  };
  document.getElementById('d-save').onclick = () => submit(false);
  document.getElementById('d-none').onclick = () => submit(true);
}

// --------------------------------------------------------------------------
// routing
// --------------------------------------------------------------------------
const ROUTES = [
  [/^\/overview$/, viewOverview],
  [/^\/concepts$/, viewConcepts],
  [/^\/concept\/(.+)$/, viewConcept],
  [/^\/vocabulary$/, viewVocabulary],
  [/^\/axis\/(.+)$/, viewAxis],
  [/^\/studies$/, viewStudies],
  [/^\/study\/(.+)$/, viewStudy],
  [/^\/spec\/(.+)$/, viewSpec],
  [/^\/usdm\/(.+)$/, viewUsdm],
  [/^\/gaps$/, viewGaps],
  [/^\/review$/, viewReview],
];

const NAV_FOR = { overview: 'overview', concepts: 'concepts', concept: 'concepts',
  vocabulary: 'vocabulary', axis: 'vocabulary', studies: 'studies', study: 'studies',
  spec: 'studies', usdm: 'studies', gaps: 'gaps', review: 'review' };

function go(path) { window.location.hash = '#' + path; }
window.go = go;

async function route() {
  const path = (window.location.hash || '#/overview').slice(1);
  const head = path.split('/')[1] || 'overview';
  document.querySelectorAll('#nav button').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === NAV_FOR[head]));

  for (const [pattern, view] of ROUTES) {
    const match = path.match(pattern);
    if (match) {
      try {
        await view(...match.slice(1).map(decodeURIComponent));
      } catch (err) {
        render(`<div class="notice"><strong>Could not load this view.</strong> ${esc(err.message)}</div>`);
      }
      return;
    }
  }
  go('/overview');
}

document.querySelectorAll('#nav button').forEach((b) => {
  b.onclick = () => go('/' + b.dataset.view);
});

window.addEventListener('hashchange', route);

(async function start() {
  try {
    const s = await api('/api/summary');
    document.getElementById('version-line').textContent =
      `${s.counts.concepts} concepts · ${s.counts.terms} terms across ${s.counts.axes} axes · ` +
      `${s.counts.studies} studies · USDM ${s.usdm_version} · ${s.derivation_version}`;
  } catch (err) {
    document.getElementById('version-line').textContent = 'database not built — run ceskb refresh';
  }
  route();
})();
