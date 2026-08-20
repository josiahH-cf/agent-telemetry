(() => {
  "use strict";

  const data = window.TELEMETRY || {};
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const sum = values => values.reduce((total, value) => total + (finite(value) ? value : 0), 0);
  const compact = new Intl.NumberFormat("en-US", {maximumFractionDigits:1, notation:"compact"});
  const full = new Intl.NumberFormat("en-US", {maximumFractionDigits:2});
  const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:2});
  const colors = ["#7bdcff", "#8ba9ff", "#75e6ad", "#ffd166", "#c4a7ff", "#ff8c9b", "#b8c5d3"];
  const catalog = new Map((data.catalog || []).map(row => [row.metric_id, row]));
  const windows = data.windows || {};
  const validWindows = data.contract && data.contract.window_keys || ["7", "30", "90", "all"];
  const params = new URLSearchParams(window.location.search);
  let activeKey = validWindows.includes(params.get("window")) ? params.get("window") : data.default_window || "30";
  let active = windows[activeKey] || Object.values(windows)[0] || {};

  function fmt(value, kind = "number", reason = "not observed") {
    if (!finite(value)) return `<span class="empty">n/a · ${esc(reason)}</span>`;
    if (kind === "money") return money.format(value);
    if (kind === "percent") return `${full.format(value * 100)}%`;
    if (kind === "tokens") return compact.format(value);
    if (kind === "minutes") return `${full.format(value)}m`;
    if (kind === "years") return `${full.format(value)} years`;
    return full.format(value);
  }

  function when(value) {
    if (!value) return "n/a · timestamp unavailable";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? "n/a · timestamp invalid" : parsed.toLocaleString([], {year:"numeric", month:"short", day:"numeric", hour:"2-digit", minute:"2-digit", timeZoneName:"short"});
  }

  function metricButton(metricId) {
    const metric = catalog.get(metricId);
    const label = metric ? metric.display_label : metricId;
    return `<button class="metric-help" type="button" data-explain="${esc(metricId)}" aria-label="Explain ${esc(label)}">i</button>`;
  }

  function card(metricId, value, detail, delta = "") {
    const metric = catalog.get(metricId) || {display_label:metricId};
    return `<article class="card" data-metric-id="${esc(metricId)}"><div class="metric-head"><span class="label">${esc(metric.display_label)}</span>${metricButton(metricId)}</div><span class="value">${value}</span><span class="detail">${detail}</span>${delta ? `<span class="delta">${delta}</span>` : ""}</article>`;
  }

  function deltaText(current, previous, kind = "number") {
    if (!finite(current) || !finite(previous)) return "↔ prior equal window unavailable";
    const delta = current - previous;
    const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "↔";
    const magnitude = Math.abs(delta);
    const value = kind === "money" ? money.format(magnitude) : kind === "percent" ? `${full.format(magnitude * 100)} pp` : kind === "tokens" ? compact.format(magnitude) : kind === "minutes" ? `${full.format(magnitude)}m` : full.format(magnitude);
    return `${arrow} ${delta > 0 ? "+" : delta < 0 ? "−" : ""}${value} vs prior equal window`;
  }

  function chartHeader(metricId, subtitle) {
    const metric = catalog.get(metricId) || {display_label:metricId};
    return `<div class="chart-title"><div><h3>${esc(metric.display_label)}</h3><span>${esc(subtitle)}</span></div>${metricButton(metricId)}</div>`;
  }

  function donut(targetId, metricId, rows, valueKey, subtitle, formatter = value => fmt(value, "tokens")) {
    const target = $(targetId);
    const clean = rows.filter(row => finite(Number(row[valueKey])) && Number(row[valueKey]) >= 0);
    const total = sum(clean.map(row => Number(row[valueKey])));
    const circumference = 2 * Math.PI * 44;
    let offset = 0;
    const circles = clean.map((row, index) => {
      const length = total ? Number(row[valueKey]) / total * circumference : 0;
      const circle = `<circle cx="50" cy="50" r="44" stroke="${colors[index % colors.length]}" stroke-dasharray="${length} ${circumference}" stroke-dashoffset="${-offset}"></circle>`;
      offset += length;
      return circle;
    }).join("");
    const legend = clean.map((row, index) => {
      const tail = row.other_count ? ` (${row.other_count} more)` : "";
      return `<div class="legend-row"><span class="legend-dot" style="background:${colors[index % colors.length]}"></span><span class="legend-name" title="${esc(row.label)}${esc(tail)}">${esc(row.label)}${esc(tail)}</span><span class="legend-value">${formatter(Number(row[valueKey]))}</span></div>`;
    }).join("");
    target.dataset.metricId = metricId;
    target.innerHTML = `${chartHeader(metricId, subtitle)}<div class="chart-body donut-layout"><svg class="donut" viewBox="0 0 100 100" role="img" aria-label="${esc(subtitle)}"><circle cx="50" cy="50" r="44" stroke="#202937"></circle>${circles}</svg><div class="legend">${legend || '<span class="empty">n/a · no measured composition</span>'}</div></div>`;
  }

  function lineChart(targetId, metricId, rows, series, subtitle) {
    const target = $(targetId);
    const maximum = Math.max(1, ...rows.flatMap(row => series.map(item => Number(row[item.key]) || 0)));
    const polylines = series.map((item, index) => {
      const points = rows.map((row, pointIndex) => {
        const x = rows.length <= 1 ? 50 : 4 + pointIndex / (rows.length - 1) * 92;
        const y = 92 - (Number(row[item.key]) || 0) / maximum * 82;
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      }).join(" ");
      return `<polyline points="${points}" stroke="${item.color || colors[index]}"></polyline>`;
    }).join("");
    const first = rows[0];
    const last = rows[rows.length - 1];
    const labels = rows.length ? `<div class="plot-labels"><span>${esc(first.from || first.date)}</span><span>${esc(last.to || last.date)}</span></div>` : "";
    const key = series.map((item, index) => `<span><b style="color:${item.color || colors[index]}">— ${esc(item.label)}</b></span>`).join("");
    target.dataset.metricId = metricId;
    target.innerHTML = `${chartHeader(metricId, subtitle)}<div class="chart-body">${rows.length ? `<svg class="plot" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="${esc(subtitle)}"><line class="axis" x1="4" y1="92" x2="96" y2="92"></line>${polylines}</svg>${labels}<div class="series-key">${key}</div>` : '<span class="empty">n/a · no points in this window</span>'}</div>`;
  }

  function ranked(targetId, metricId, rows, valueKey, subtitle, formatter, color = colors[0]) {
    const target = $(targetId);
    const maximum = Math.max(1, ...rows.map(row => Number(row[valueKey]) || 0));
    const body = rows.map(row => {
      const label = row.other_count ? `other (${row.other_count} more)` : row.label;
      return `<div class="rank-row"><span class="rank-name" title="${esc(label)}">${esc(label)}</span><span class="track" aria-hidden="true"><span class="fill" style="width:${(Number(row[valueKey]) || 0) / maximum * 100}%;background:${color}"></span></span><span class="rank-value">${formatter(Number(row[valueKey]) || 0)}</span></div>`;
    }).join("");
    target.dataset.metricId = metricId;
    target.innerHTML = `${chartHeader(metricId, subtitle)}<div class="chart-body ranked">${body || '<span class="empty">n/a · no ranked records</span>'}</div>`;
  }

  function status(targetId, ok, goodText, badText, warning = false) {
    const target = $(targetId);
    target.className = `status ${ok ? (warning ? "warn" : "good") : "bad"}`;
    target.textContent = ok ? goodText : badText;
  }

  function ageMinutes() {
    const generated = Date.parse(data.generated_at);
    return Number.isFinite(generated) ? Math.max(0, (Date.now() - generated) / 60000) : null;
  }

  function renderMasthead() {
    const age = ageMinutes();
    $("mast-meta").innerHTML = `<span data-metric-id="data_age_minutes">${finite(age) ? `${full.format(age)}m old` : "age n/a"} ${metricButton("data_age_minutes")}</span><br>Generated ${esc(when(data.generated_at))}`;
  }

  function renderOverview() {
    const point = data.point_in_time || {};
    const totals = point.totals || {};
    $("overview-cards").innerHTML = [
      card("lifetime_sessions", fmt(totals.sessions), "Across both providers and host operating systems"),
      card("lifetime_tokens", fmt(totals.tokens, "tokens"), "Provider-correct lifetime total"),
      card("lifetime_cost_usd", fmt(totals.cost_usd, "money"), "Exact observed models only"),
      card("lifetime_unpriced_tokens", fmt(totals.unpriced_tokens, "tokens"), "Counted, never silently priced"),
    ].join("");
    donut("vendor-chart", "tokens_by_vendor", point.by_vendor || [], "tokens", "Lifetime · Anthropic vs OpenAI");
    donut("host-chart", "tokens_by_host_os", point.by_host_os || [], "tokens", "Lifetime · WSL-hosted vs Windows-hosted");
    status("overview-check", point.reconciliation === "ok" && point.store_integrity === "ok", "Store · page · machine agree", "Reconciliation needs attention");
  }

  function renderActivity() {
    const summary = active.summary || {};
    const prior = (active.comparison || {}).summary || {};
    $("activity-cards").innerHTML = [
      card("window_tokens", fmt(summary.tokens, "tokens"), `${esc(active.from)} through ${esc(active.to)} UTC`, deltaText(summary.tokens, prior.tokens, "tokens")),
      card("window_cost_usd", fmt(summary.cost_usd, "money"), "Unpriced usage remains separate", deltaText(summary.cost_usd, prior.cost_usd, "money")),
      card("window_session_days", fmt(summary.session_days), "Daily session presences, not unique sessions", deltaText(summary.session_days, prior.session_days)),
      card("window_active_days", fmt(summary.active_days), "Non-zero daily rollups", deltaText(summary.active_days, prior.active_days)),
    ].join("");
    lineChart("token-trend", "daily_tokens", active.daily || [], [{key:"tokens", label:"tokens per bucket", color:colors[0]}], `${active.from} → ${active.to} · exact total, ≤48 buckets`);
    lineChart("cost-trend", "daily_cost_usd", active.daily || [], [{key:"cost_usd", label:"exact USD per bucket", color:colors[1]}], `${active.from} → ${active.to} · API-equivalent USD`);
  }

  function renderMix() {
    const point = data.point_in_time || {};
    const projectRows = active.top_projects || [];
    const buckets = active.bucket_tokens || {};
    const prior = active.comparison || {};
    const priorBuckets = prior.bucket_tokens || {};
    $("mix-cards").innerHTML = [
      card("window_project_identities", fmt(active.project_count), "Distinct identities active in the exact window", deltaText(active.project_count, prior.project_count)),
      card("window_ad_hoc_tokens", fmt(buckets["ad-hoc"] || 0, "tokens"), "Explicit non-project bulk bucket", deltaText(buckets["ad-hoc"] || 0, priorBuckets["ad-hoc"], "tokens")),
      card("window_remote_tokens", fmt(buckets["remote"] || 0, "tokens"), "Explicit remote bulk bucket", deltaText(buckets["remote"] || 0, priorBuckets["remote"], "tokens")),
      card("unregistered_candidates", fmt(point.unregistered_candidates), "Current anonymous clusters · point-in-time"),
    ].join("");
    donut("project-chart", "tokens_by_project", projectRows, "tokens", `${active.from} → ${active.to} · top 6 + exact other`);
    ranked("model-chart", "tokens_by_model", point.top_models || [], "tokens", "Lifetime · observed session-model buckets", value => fmt(value, "tokens"), colors[1]);
    refreshLazy("project-detail");
  }

  function renderOutcomes() {
    const outcome = active.outcomes || {};
    const prior = (active.comparison || {}).outcomes || {};
    $("outcome-cards").innerHTML = [
      card("accepted_features", fmt(outcome.accepted_features), "Distinct accepted specs", deltaText(outcome.accepted_features, prior.accepted_features)),
      card("acceptance_efficiency", fmt(outcome.acceptance_efficiency, "percent", "no specs in window"), "Accepted specs / represented specs", deltaText(outcome.acceptance_efficiency, prior.acceptance_efficiency, "percent")),
      card("mean_cost_per_accepted", fmt(outcome.mean_cost_per_accepted, "money", "no accepted feature in window"), "Loop exact cost / accepted feature", deltaText(outcome.mean_cost_per_accepted, prior.mean_cost_per_accepted, "money")),
      card("median_round_minutes", fmt(outcome.median_round_minutes, "minutes", "no complete rounds"), "Clamp anomalies remain counted in the full envelope", deltaText(outcome.median_round_minutes, prior.median_round_minutes, "minutes")),
    ].join("");
    lineChart("round-outcome-chart", "rounds_by_day", active.rounds_by_day || [], [
      {key:"accepted", label:"accepted rounds", color:colors[2]},
      {key:"not_accepted", label:"non-accepted rounds", color:colors[3]},
    ], `${active.from} → ${active.to} · completion date`);
    ranked("spec-cost-chart", "spec_cost_rank", active.top_specs || [], "cost_usd", `${active.from} → ${active.to} · top 6 + exact other`, value => fmt(value, "money"), colors[2]);
    refreshLazy("spec-detail");
  }

  function renderReliability() {
    const point = data.point_in_time || {};
    const cadence = point.cadence || {};
    const disk = point.disk || {};
    const doctor = point.doctor || {};
    const measurement = active.measurement || {};
    $("reliability-cards").innerHTML = [
      card("data_age_minutes", fmt(ageMinutes(), "minutes", "generation timestamp missing"), "Updates in this page without a reload"),
      card("doctor_status", esc(doctor.status || "unknown"), "Latest self-check result"),
      card("missed_intervals", fmt(cadence.missed_intervals), "Derived from observed wrapper starts"),
      card("disk_runway_years", fmt(disk.runway_years, "years", "disk snapshot unavailable"), "Shorter conservative drive bound"),
    ].join("");
    const roots = point.roots || [];
    const complete = roots.filter(root => root.status === "ok" && !root.file_errors).length;
    donut("root-chart", "source_root_status", [{label:"complete", roots:complete}, {label:"partial / other", roots:Math.max(0, roots.length - complete)}], "roots", "Current completeness · usable partial roots stay explicit", value => `${fmt(value)} roots`);
    ranked("probe-chart", "measurement_probe_health", [{label:"healthy", probes:measurement.healthy || 0}, {label:"other", probes:Math.max(0, (measurement.total || 0) - (measurement.healthy || 0))}], "probes", `${active.from} → ${active.to} · observed collection probes`, value => fmt(value), colors[2]);
    status("reliability-check", doctor.status !== "fail", doctor.status === "ok" ? "Doctor green" : "Doctor warning", "Doctor failed", doctor.status === "warn");
    refreshLazy("diagnostic-detail");
  }

  function renderEvidence() {
    const outcome = active.outcomes || {};
    const prior = (active.comparison || {}).outcomes || {};
    $("evidence-cards").innerHTML = [
      card("window_rounds", fmt(outcome.rounds), "Complete judge rounds in the exact window", deltaText(outcome.rounds, prior.rounds)),
      card("window_accepted_rounds", fmt(outcome.accepted_rounds), "Acceptance verdicts at round level", deltaText(outcome.accepted_rounds, prior.accepted_rounds)),
      card("window_findings", fmt(outcome.findings), "Structured blocking findings", deltaText(outcome.findings, prior.findings)),
      card("window_loop_cost_usd", fmt(outcome.cost_usd, "money"), "Unpriced loop usage remains separate", deltaText(outcome.cost_usd, prior.cost_usd, "money")),
    ].join("");
    lineChart("duration-chart", "round_duration_trend", active.rounds_by_day || [], [{key:"median_round_minutes", label:"median minutes", color:colors[4]}], `${active.from} → ${active.to} · wall clock incl. queue idle`);
    ranked("recent-chart", "recent_spec_ledger", (active.recent_specs || []).map(row => ({label:row.spec, rounds:row.rounds})), "rounds", `${active.from} → ${active.to} · six most recently completed specs`, value => `${fmt(value)} rounds`, colors[4]);
    status("evidence-check", (outcome.accepted_rounds || 0) <= (outcome.rounds || 0) && (active.recent_specs || []).length <= 6, "Window joins reconcile", "Evidence totals differ");
    refreshLazy("ledger-detail");
  }

  function table(headers, rows) {
    return `<div class="table-wrap"><table><thead><tr>${headers.map(([label, numeric, metricId]) => `<th${numeric ? ' class="num"' : ""}>${esc(label)}${metricId ? ` ${metricButton(metricId)}` : ""}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }

  function lazyBody(detailId) {
    const body = $(detailId).querySelector("[data-lazy-body]");
    if (detailId === "project-detail") {
      const rows = (active.top_projects || []).map(row => `<tr><td>${esc(row.label)}${row.other_count ? ` (${fmt(row.other_count)} more)` : ""}</td><td class="num">${fmt(row.tokens, "tokens")}</td></tr>`);
      body.innerHTML = table([["Identity",false],["Tokens",true,"tokens_by_project"]], rows);
    } else if (detailId === "spec-detail") {
      const rows = (active.top_specs || []).map(row => `<tr><td>${esc(row.label)}${row.other_count ? ` (${fmt(row.other_count)} more)` : ""}</td><td class="num">${fmt(row.cost_usd, "money")}</td></tr>`);
      body.innerHTML = table([["Feature / tail",false],["Exact USD",true,"spec_cost_rank"]], rows);
    } else if (detailId === "diagnostic-detail") {
      const point = data.point_in_time || {};
      const checks = ((point.doctor || {}).checks || []).map(row => `<tr><td>${esc(row.name)}</td><td>${esc(row.status)}</td><td>${esc(row.detail)}</td></tr>`);
      const roots = (point.roots || []).map(row => `<tr><td>${esc(row.root_id)}</td><td>${esc(row.status)}</td><td>${esc(when(row.last_success_at))}</td></tr>`);
      body.innerHTML = `<h3>Doctor checks</h3>${table([["Check",false],["Status",false,"doctor_status"],["Sanitized detail",false]], checks)}<h3 style="margin-top:14px">Provider roots</h3>${table([["Root",false],["Status",false,"source_root_status"],["Last successful scan",false,"source_root_status"]], roots)}`;
    } else if (detailId === "ledger-detail") {
      const rows = (active.recent_specs || []).map(row => `<tr><td>${esc(row.spec)}</td><td>${esc(row.outcome)}</td><td class="num">${fmt(row.rounds)}</td><td class="num">${fmt(row.tokens, "tokens")}</td><td class="num">${fmt(row.cost_usd, "money")}</td><td class="num">${fmt(row.findings)}</td><td>${esc(when(row.latest_at))}</td></tr>`);
      body.innerHTML = table([["Feature",false],["Outcome",false,"recent_spec_ledger"],["Rounds",true,"recent_spec_ledger"],["Tokens",true,"recent_spec_ledger"],["Exact USD",true,"recent_spec_ledger"],["Findings",true,"recent_spec_ledger"],["Latest",false,"recent_spec_ledger"]], rows);
    }
    body.dataset.built = "true";
  }

  function refreshLazy(detailId) {
    const detail = $(detailId);
    const body = detail.querySelector("[data-lazy-body]");
    body.replaceChildren();
    body.dataset.built = "false";
    if (detail.open) lazyBody(detailId);
  }

  function showMetric(metricId) {
    const metric = catalog.get(metricId);
    if (!metric) return;
    $("metric-dialog-title").textContent = metric.display_label;
    $("metric-dialog-body").innerHTML = `<p>${esc(metric.definition)}</p><div class="formula">${esc(metric.derivation)}</div><p class="catalog-meta"><strong>Unit:</strong> ${esc(metric.unit)}<br><strong>Source:</strong> ${metric.sources.map(esc).join(" · ")}<br><strong>Caveat:</strong> ${esc(metric.caveats)}<br><strong>Catalog id:</strong> ${esc(metric.metric_id)}</p>`;
    $("metric-dialog").showModal();
  }

  function updateTestHook() {
    const rendered = [...document.querySelectorAll("[data-metric-id]")].map(node => node.dataset.metricId).filter(Boolean);
    window.__AGENT_TELEMETRY_TEST__ = {
      activeWindow: activeKey,
      renderedMetricIds: [...new Set(rendered)].sort(),
      catalogPageMetricIds: (data.catalog || []).filter(row => row.surface === "page").map(row => row.metric_id).sort(),
      atRest: {
        totalElements: document.querySelectorAll("body *").length,
        openDrilldowns: document.querySelectorAll("details.drill[open]").length,
        materializedRows: document.querySelectorAll("details.drill tbody tr").length,
        trendPolylines: document.querySelectorAll(".plot polyline").length,
        rankRows: document.querySelectorAll(".rank-row").length,
      },
      payloadBytes: data.contract && data.contract.payload_bytes,
    };
  }

  function render() {
    active = windows[activeKey] || active;
    document.querySelectorAll("[data-window]").forEach(button => button.setAttribute("aria-pressed", button.dataset.window === activeKey ? "true" : "false"));
    $("window-summary").textContent = `${active.from} → ${active.to} · ${active.inclusive_days} inclusive UTC days · exact precomputed ${activeKey === "all" ? "all-history" : `${activeKey}-day`} view`;
    renderMasthead();
    renderOverview();
    renderActivity();
    renderMix();
    renderOutcomes();
    renderReliability();
    renderEvidence();
    $("generated-foot").textContent = `Generated ${when(data.generated_at)} · compact page is within its published budget · full envelope and all machine URLs retained · no runtime network requests.`;
    updateTestHook();
  }

  document.querySelectorAll("[data-window]").forEach(button => button.addEventListener("click", () => {
    activeKey = button.dataset.window;
    const url = new URL(window.location.href);
    url.searchParams.delete("from");
    url.searchParams.delete("to");
    url.searchParams.set("window", activeKey);
    window.history.replaceState({}, "", url);
    render();
  }));
  document.querySelectorAll("details.drill").forEach(detail => detail.addEventListener("toggle", () => {
    const body = detail.querySelector("[data-lazy-body]");
    if (detail.open && body.dataset.built !== "true") lazyBody(detail.id);
    updateTestHook();
  }));
  document.addEventListener("click", event => {
    const button = event.target.closest && event.target.closest("[data-explain]");
    if (button) showMetric(button.dataset.explain);
  });
  $("metric-dialog-close").addEventListener("click", () => $("metric-dialog").close());

  if (data.payload_kind !== "bounded_page_envelope") {
    document.body.innerHTML = '<main class="shell"><section><h1>Telemetry unavailable</h1><p class="empty">The bounded page envelope is missing or incompatible.</p></section></main>';
    return;
  }
  render();
  window.setInterval(() => { renderMasthead(); const cardNode = document.querySelector('[data-metric-id="data_age_minutes"] .value'); if (cardNode) cardNode.innerHTML = fmt(ageMinutes(), "minutes"); }, 60000);
})();
