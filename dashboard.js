const AgentTelemetryUI = (() => {
  "use strict";

  const finiteNumber = value => typeof value === "number" && Number.isFinite(value);
  const numericValue = value => {
    if (typeof value === "number") return Number.isFinite(value) ? value : NaN;
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : NaN;
    }
    return NaN;
  };
  const parsedMillis = value => {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const captureStatusFailed = value =>
    /(fail|error|timeout|denied|malformed|invalid)/.test(String(value || "").toLowerCase());

  function relativeDuration(value, nowMillis = Date.now()) {
    const target = parsedMillis(value);
    if (target === null || !finiteNumber(nowMillis)) return null;
    const minutes = Math.max(0, Math.floor(Math.abs(target - nowMillis) / 60000));
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor((minutes % 1440) / 60);
    const remainder = minutes % 60;
    const parts = [];
    if (days) parts.push(`${days}d`);
    if (hours && parts.length < 2) parts.push(`${hours}h`);
    if ((!days || !hours) && parts.length < 2) parts.push(`${remainder}m`);
    return {direction:target >= nowMillis ? "future" : "past", text:parts.join(" ") || "0m", minutes};
  }

  function capacityWindowState(windowValue, nowMillis = Date.now(), freshnessMaxAgeHours = null) {
    const value = windowValue && typeof windowValue === "object" ? windowValue : {};
    const remaining = numericValue(value.remaining_percent);
    const hasValue = Number.isFinite(remaining) && remaining >= 0 && remaining <= 100;
    const raw = String(value.freshness_status || "").toLowerCase().replace(/[- ]/g, "_");
    const capture = String(value.capture_status || "").toLowerCase();
    const captureFailed = captureStatusFailed(capture);
    const observed = parsedMillis(value.observed_at);
    const configuredAge = numericValue(freshnessMaxAgeHours);
    const ageBoundary = observed !== null && Number.isFinite(configuredAge) && configuredAge >= 0
      ? observed + configuredAge * 3600000
      : parsedMillis(value.fresh_until);
    const reset = parsedMillis(value.resets_at);
    const freshnessUnknown = observed === null || ageBoundary === null;
    const observationFuture = observed !== null && observed > nowMillis;
    const ageExpired = ageBoundary !== null && ageBoundary <= nowMillis;
    const resetPassed = reset !== null && reset <= nowMillis && (observed === null || observed <= reset);
    let state = raw === "fresh" ? "available" : raw;
    if (state === "retained" || state === "last_good") state = "retained_last_good";
    if (state === "capture_error") state = "error";
    if (!hasValue) {
      state = state === "error" || captureFailed ? "error" : "unavailable";
    } else if (state === "stale" || observationFuture || ageExpired || resetPassed || freshnessUnknown) {
      state = "stale";
    } else if (state === "retained_last_good" || state === "error" || captureFailed) {
      state = "retained_last_good";
    } else if (state === "available") {
      state = "available";
    } else {
      state = "stale";
    }
    return {state, hasValue, remainingPercent:hasValue ? remaining : null, ageExpired, resetPassed, freshnessUnknown, observationFuture};
  }

  function capacityProviderState(providerValue, nowMillis = Date.now()) {
    const provider = providerValue && typeof providerValue === "object" ? providerValue : {};
    return capacityWindowState(
      {
        remaining_percent:null,
        freshness_status:provider.freshness_status || provider.quota_status || provider.remaining_status,
        capture_status:provider.capture_status,
        observed_at:provider.observed_at,
        age_hours:provider.age_hours,
      },
      nowMillis,
      provider.freshness_max_age_hours,
    );
  }

  function calculateScenario(input, project) {
    const assumptions = input && typeof input === "object" ? input : {};
    const selected = project && typeof project === "object" ? project : {};
    const recorded = numericValue(selected.recorded_attention_hours);
    const manual = numericValue(assumptions.counterfactual_manual_hours);
    const valuePerHour = numericValue(assumptions.value_of_attention_usd_per_hour);
    const actualCash = numericValue(assumptions.actual_cash_usd);
    const displacedShare = numericValue(assumptions.displaced_share_percent);
    const alternativeValue = numericValue(assumptions.alternative_value_usd_per_hour);
    const apiEquivalent = numericValue(selected.api_equivalent_cost_usd);
    const cashBasis = String(assumptions.cash_basis || "");
    const alternativeName = String(assumptions.alternative_name || "").trim();
    const validBasis = ["none", "api_equivalent", "actual_cash"].includes(cashBasis);
    const valid = Number.isFinite(recorded) && recorded > 0
      && Number.isFinite(manual) && manual >= 0
      && Number.isFinite(valuePerHour) && valuePerHour > 0
      && validBasis
      && (cashBasis !== "api_equivalent" || (Number.isFinite(apiEquivalent) && apiEquivalent >= 0))
      && (cashBasis !== "actual_cash" || (Number.isFinite(actualCash) && actualCash >= 0))
      && alternativeName.length > 0
      && Number.isFinite(displacedShare) && displacedShare >= 0 && displacedShare <= 100
      && Number.isFinite(alternativeValue) && alternativeValue >= 0;
    if (!valid) return {valid:false};
    const cashUsd = cashBasis === "api_equivalent" ? apiEquivalent : cashBasis === "actual_cash" ? actualCash : 0;
    const displacedAttentionHours = recorded * displacedShare / 100;
    const attentionDeltaHours = manual - recorded;
    const attentionEquivalentHours = recorded + cashUsd / valuePerHour;
    const opportunityCostUsd = displacedAttentionHours * alternativeValue;
    if (![cashUsd, displacedAttentionHours, attentionDeltaHours, attentionEquivalentHours, opportunityCostUsd].every(Number.isFinite)) {
      return {valid:false};
    }
    return {
      valid:true,
      recordedAttentionHours:recorded,
      attentionDeltaHours,
      attentionEquivalentHours,
      displacedAttentionHours,
      opportunityCostUsd,
      cashBasis,
      cashUsd,
      alternativeName,
      displacedSharePercent:displacedShare,
      alternativeValueUsdPerHour:alternativeValue,
    };
  }

  return Object.freeze({capacityProviderState, capacityWindowState, captureStatusFailed, calculateScenario, relativeDuration});
})();

if (typeof window !== "undefined") window.AgentTelemetryUI = AgentTelemetryUI;
if (typeof module === "object" && module.exports) module.exports = AgentTelemetryUI;

(() => {
  "use strict";

  if (typeof window === "undefined" || typeof document === "undefined") return;

  const data = window.TELEMETRY || {};
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const numeric = value => typeof value === "number" && Number.isFinite(value) ? value : typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
  const sum = values => values.reduce((total, value) => total + (finite(value) ? value : 0), 0);
  const compact = new Intl.NumberFormat("en-US", {maximumFractionDigits:1, notation:"compact"});
  const full = new Intl.NumberFormat("en-US", {maximumFractionDigits:2});
  const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:2});
  const percentNumber = new Intl.NumberFormat("en-US", {maximumFractionDigits:1});
  const colors = ["#7bdcff", "#8ba9ff", "#75e6ad", "#ffd166", "#c4a7ff", "#ff8c9b", "#b8c5d3"];
  const {capacityProviderState, capacityWindowState, calculateScenario, relativeDuration} = AgentTelemetryUI;
  const catalog = new Map((data.catalog || []).map(row => [row.metric_id, row]));
  const windows = data.windows || {};
  const validWindows = data.contract && data.contract.window_keys || ["7", "30", "90", "all"];
  const params = new URLSearchParams(window.location.search);
  let activeKey = validWindows.includes(params.get("window")) ? params.get("window") : data.default_window || "30";
  let active = windows[activeKey] || Object.values(windows)[0] || {};
  let scenarioProjects = [];

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

  function timeMarkup(value) {
    if (!value || !Number.isFinite(Date.parse(value))) return '<span class="empty">timestamp unavailable</span>';
    return `<time datetime="${esc(value)}">${esc(when(value))}</time>`;
  }

  function evidenceBadge(evidenceClass, display = "") {
    const normalized = String(evidenceClass || "unknown").toLowerCase().replace(/[^a-z-]/g, "-");
    const label = display || normalized.replace(/-/g, " ");
    return `<span class="evidence-badge evidence-${esc(normalized)}">${esc(label)}</span>`;
  }

  function metricButton(metricId) {
    const metric = catalog.get(metricId);
    const label = metric ? metric.display_label : metricId;
    return `<button class="metric-help" type="button" data-explain="${esc(metricId)}" aria-label="Explain ${esc(label)}">i</button>`;
  }

  function card(metricId, value, detail, delta = "", evidenceOverride = "") {
    const metric = catalog.get(metricId) || {display_label:metricId};
    const evidence = evidenceOverride || metric.evidence_class;
    return `<article class="card" data-metric-id="${esc(metricId)}"><div class="metric-head"><span class="metric-label-group"><span class="label">${esc(metric.display_label)}</span>${evidence ? evidenceBadge(evidence, evidence === "observed" && metricId === "recorded_operator_attention_hours" ? "Recorded" : "") : ""}</span>${metricButton(metricId)}</div><span class="value">${value}</span><span class="detail">${detail}</span>${delta ? `<span class="delta">${delta}</span>` : ""}</article>`;
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

  function relativeObservation(value, ageHours = null) {
    const relative = relativeDuration(value);
    if (relative) return `Observed ${relative.direction === "future" ? "at a future client-clock time" : `${relative.text} ago`}`;
    const age = numeric(ageHours);
    return age !== null && age >= 0 ? `Observed about ${full.format(age)}h ago` : "Observation time unavailable";
  }

  function resetDescription(value, observedAt = null) {
    if (!value || !Number.isFinite(Date.parse(value))) return "Reset not reported.";
    const relative = relativeDuration(value);
    if (!relative) return `Reset ${timeMarkup(value)}`;
    const observed = Date.parse(observedAt);
    const reset = Date.parse(value);
    const observationIsNewer = Number.isFinite(observed) && Number.isFinite(reset) && observed > reset;
    const status = relative.direction === "future"
      ? `Resets in ${relative.text}`
      : observationIsNewer
        ? `Reported reset passed ${relative.text} ago; observation is newer`
        : `Reset passed ${relative.text} ago`;
    return `${status} · ${timeMarkup(value)}`;
  }

  function capacityStateText(state, windowValue) {
    const observed = relativeObservation(windowValue.observed_at, windowValue.age_hours);
    const originalState = String(windowValue.freshness_status || "").toLowerCase().replace(/[- ]/g, "_");
    const capture = String(windowValue.capture_status || "").toLowerCase();
    const retainedAfterFailure = originalState === "retained_last_good" || AgentTelemetryUI.captureStatusFailed(capture);
    if (state === "available") return `Fresh — ${observed.toLowerCase()}.`;
    if (state === "stale" && retainedAfterFailure) return `Stale — last reported ${observed.replace(/^Observed /, "").toLowerCase()}; latest capture failed.`;
    if (state === "stale") return `Stale — last ${observed.toLowerCase()}.`;
    if (state === "retained_last_good") return `Last reported ${observed.replace(/^Observed /, "").toLowerCase()}; latest capture failed.`;
    if (state === "error") return "Capture error — latest capture failed and no usable last-good value exists.";
    return "Unavailable — no valid value has ever been observed.";
  }

  function capacityWindowMarkup(windowValue, provider, index) {
    const freshnessMaxAgeHours = provider.freshness_max_age_hours;
    const evaluated = capacityWindowState(windowValue, Date.now(), freshnessMaxAgeHours);
    const remaining = evaluated.hasValue ? `${percentNumber.format(evaluated.remainingPercent)}% remaining` : "Remaining unavailable";
    const usedPercent = numeric(windowValue.used_percent);
    const minutes = numeric(windowValue.window_minutes);
    const used = usedPercent !== null && usedPercent >= 0 && usedPercent <= 100
      ? `${percentNumber.format(usedPercent)}% used as reported`
      : "Used percentage not reported";
    const windowMinutes = minutes !== null && minutes > 0
      ? ` · ${full.format(minutes)} minute window`
      : "";
    const metricId = /(anthropic|claude)/.test(String(windowValue.provider || provider.provider || "").toLowerCase())
      ? "claude_quota_remaining_percent"
      : "openai_quota_remaining_percent";
    const progress = evaluated.hasValue
      ? `<progress max="100" value="${evaluated.remainingPercent}" aria-label="${esc(windowValue.display_label || windowValue.window || `window ${index + 1}`)}: ${esc(remaining)}"></progress>`
      : "";
    const observedTime = windowValue.observed_at && Number.isFinite(Date.parse(windowValue.observed_at))
      ? ` · ${timeMarkup(windowValue.observed_at)}`
      : "";
    return `<article class="capacity-window" data-capacity-state="${esc(evaluated.state)}" data-metric-id="${metricId}"><div class="capacity-window-head"><div><h3>${esc(windowValue.display_label || windowValue.window || `Window ${index + 1}`)}</h3><span class="detail">${esc(used)}${esc(windowMinutes)}</span></div><span>${evidenceBadge("observed")} ${metricButton(metricId)}</span></div><div class="capacity-remaining">${esc(remaining)}</div>${progress}<div class="capacity-times"><span>${capacityStateText(evaluated.state, windowValue)}${observedTime}</span><span>${resetDescription(windowValue.resets_at, windowValue.observed_at)}</span></div><details class="capacity-source"><summary>Source and capture</summary><p>Source: ${esc(windowValue.source || "not reported")} · capture: ${esc(windowValue.capture_status || "not reported")} · freshness at generation: ${esc(windowValue.freshness_status || "not reported")}. This is provider-reported capacity, not billing or an estimate of messages remaining.</p></details></article>`;
  }

  function capacityProviderEmptyMarkup(provider) {
    const evaluated = capacityProviderState(provider, Date.now());
    const metricId = String(provider.provider || "").toLowerCase() === "anthropic"
      ? "claude_quota_remaining_percent"
      : "openai_quota_remaining_percent";
    const observedTime = provider.observed_at && Number.isFinite(Date.parse(provider.observed_at))
      ? ` · ${timeMarkup(provider.observed_at)}`
      : "";
    return `<div class="capacity-window capacity-empty" data-capacity-state="${esc(evaluated.state)}" data-metric-id="${metricId}"><p class="empty">${esc(capacityStateText(evaluated.state, provider))}${observedTime} ${metricButton(metricId)}</p><details class="capacity-source"><summary>Source and capture</summary><p>Source: ${esc(provider.source || "not reported")} · capture: ${esc(provider.capture_status || "not reported")} · freshness at generation: ${esc(provider.freshness_status || provider.quota_status || "not reported")}. No valid quota window is available to display.</p></details></div>`;
  }

  function renderCapacity() {
    const capacity = data.capacity_now && typeof data.capacity_now === "object" ? data.capacity_now : {};
    const providers = Array.isArray(capacity.providers) ? capacity.providers.slice(0, 2) : [];
    const renderedStates = [];
    const rows = providers.map(provider => {
      const windowsForProvider = Array.isArray(provider.windows) ? provider.windows.slice(0, 2) : [];
      const reported = Math.max(windowsForProvider.length, Number(provider.reported_window_count) || 0);
      const additional = Math.max(0, Number(provider.additional_windows) || reported - windowsForProvider.length);
      windowsForProvider.forEach(windowValue => renderedStates.push(capacityWindowState(windowValue, Date.now(), provider.freshness_max_age_hours).state));
      if (!windowsForProvider.length) renderedStates.push(capacityProviderState(provider, Date.now()).state);
      const windowMarkup = windowsForProvider.length
        ? windowsForProvider.map((windowValue, index) => capacityWindowMarkup(windowValue, provider, index)).join("")
        : capacityProviderEmptyMarkup(provider);
      const additionalText = additional > 0 ? `${full.format(additional)} additional reported window${additional === 1 ? "" : "s"} omitted from this bounded page.` : "At most two windows are shown.";
      return `<article class="capacity-provider"><div class="capacity-provider-head"><div><h3>${esc(provider.display_label || provider.provider || "Provider")}</h3><span class="detail">${esc(additionalText)}</span></div>${evidenceBadge("observed")}</div><div class="capacity-windows">${windowMarkup}</div></article>`;
    });
    $("capacity-providers").innerHTML = rows.join("") || '<p class="empty">Provider capacity is unavailable for this generated snapshot.</p>';
    const check = $("capacity-check");
    const bad = renderedStates.includes("error");
    const warning = renderedStates.some(value => ["stale", "retained_last_good", "unavailable"].includes(value)) || !renderedStates.length;
    check.className = `status ${bad ? "bad" : warning ? "warn" : "good"}`;
    check.textContent = bad ? "Capture error" : warning ? "Capacity partial" : "Capacity fresh";
  }

  function refreshCapacityPreservingInteraction() {
    const capacityRoot = $("capacity-now");
    const focusableSelector = "button,summary,a[href],input,select,textarea,[tabindex]:not([tabindex='-1'])";
    const focusables = [...capacityRoot.querySelectorAll(focusableSelector)];
    const focusIndex = capacityRoot.contains(document.activeElement)
      ? focusables.indexOf(document.activeElement)
      : -1;
    const openDetails = [...capacityRoot.querySelectorAll("details")]
      .map((detail, index) => detail.open ? index : -1)
      .filter(index => index >= 0);
    renderCapacity();
    const refreshedDetails = [...capacityRoot.querySelectorAll("details")];
    openDetails.forEach(index => { if (refreshedDetails[index]) refreshedDetails[index].open = true; });
    if (focusIndex >= 0) {
      const refreshedFocusables = [...capacityRoot.querySelectorAll(focusableSelector)];
      if (refreshedFocusables[focusIndex]) refreshedFocusables[focusIndex].focus({preventScroll:true});
    }
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

  function attentionHours(value, reason = "no recorded attention") {
    return finite(value) ? `${full.format(value)}h` : `<span class="empty">n/a · ${esc(reason)}</span>`;
  }

  function setAttentionEmpty(message) {
    $("attention-state").textContent = message;
    const reason = message === "Attention publication is disabled." ? "publication disabled" : "attention unavailable";
    $("attention-cards").innerHTML = [
      card("recorded_operator_attention_hours", attentionHours(null, reason), "Completed operator-started timer intervals", "", "observed"),
      card("recorded_stewardship_attention_hours", attentionHours(null, reason), "Guide + review + rework modes", "", "derived"),
      card("recorded_project_transitions", fmt(null, "number", reason), "Recorded destination changes; no time penalty attached", "", "derived"),
      card("recorded_attention_dropoff_projects", fmt(null, "number", reason), "Previously attended projects with no recorded attention.", "", "derived"),
    ].join("");
    $("attention-secondary").innerHTML = `<span data-metric-id="recorded_rework_attention_hours"><strong>Recorded rework:</strong> ${attentionHours(null, reason)} ${metricButton("recorded_rework_attention_hours")} ${evidenceBadge("derived")}</span><span data-metric-id="recorded_rework_share"><strong>Rework share:</strong> ${fmt(null, "percent", reason)} ${metricButton("recorded_rework_share")} ${evidenceBadge("derived")}</span><span data-metric-id="attention_top_project_share"><strong>Top-project attention share:</strong> ${fmt(null, "percent", reason)} ${metricButton("attention_top_project_share")} ${evidenceBadge("derived")}</span>`;
    $("attention-modes").parentElement.dataset.metricId = "attention_mode_composition";
    $("attention-modes").innerHTML = modeCompositionMarkup([]);
    $("attention-ledger").parentElement.dataset.metricId = "attention_project_ledger";
    $("attention-ledger").innerHTML = '<p class="empty">No project resource rows are available for this window.</p>';
    populateScenarioProjects([]);
  }

  function modeCompositionMarkup(rows) {
    const byMode = new Map((Array.isArray(rows) ? rows : []).filter(row => row && typeof row === "object").map(row => [String(row.mode), row]));
    return ["plan", "guide", "review", "rework", "direct"].map(mode => {
      const row = byMode.get(mode) || {};
      const seconds = numeric(row.seconds);
      const hours = numeric(row.hours);
      const share = numeric(row.share);
      const width = share === null ? 0 : Math.max(0, Math.min(100, share * 100));
      const text = seconds === null
        ? "n/a"
        : `${full.format(seconds)}s · ${hours === null ? full.format(seconds / 3600) : full.format(hours)}h · ${share === null ? "share n/a" : `${percentNumber.format(share * 100)}%`}`;
      return `<div class="mode-row"><span class="mode-name">${esc(mode)}</span><span class="mode-track" aria-hidden="true"><span class="mode-fill" style="width:${width}%"></span></span><span class="mode-value">${esc(text)}</span></div>`;
    }).join("");
  }

  function attentionLedgerMarkup(rows) {
    if (!rows.length) return '<p class="empty">No project resource rows are available for this window.</p>';
    const body = rows.map(row => {
      const name = row.other_count ? `other (${full.format(row.other_count)} projects)` : row.label || row.project_id || "unknown";
      return `<tr><td>${esc(name)}</td><td class="num">${attentionHours(numeric(row.recorded_attention_hours))}</td><td class="num">${attentionHours(numeric(row.stewardship_hours))}</td><td class="num">${attentionHours(numeric(row.rework_hours))}</td><td class="num">${fmt(numeric(row.transitions_in))}</td><td class="num">${fmt(numeric(row.api_equivalent_cost_usd), "money")}</td><td class="num">${fmt(numeric(row.unpriced_tokens), "tokens")}</td></tr>`;
    }).join("");
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable project attention and cost resource ledger"><table><thead><tr><th>Project / tail</th><th class="num">Recorded attention (h) ${evidenceBadge("observed", "Recorded")}</th><th class="num">Stewardship (h) ${evidenceBadge("derived")}</th><th class="num">Rework (h) ${evidenceBadge("derived")}</th><th class="num">Transitions in ${evidenceBadge("derived")}</th><th class="num">API-equivalent USD ${evidenceBadge("derived")}</th><th class="num">Unpriced tokens ${evidenceBadge("observed")}</th></tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function populateScenarioProjects(rows) {
    scenarioProjects = rows.filter(row => {
      const label = String(row.label || row.project_id || "").toLowerCase();
      return !row.other_count && label !== "other" && finite(numeric(row.recorded_attention_hours)) && numeric(row.recorded_attention_hours) > 0;
    });
    const select = $("scenario-project");
    select.innerHTML = '<option value="">Choose a displayed project</option>' + scenarioProjects.map((row, index) => `<option value="${index}">${esc(row.label || row.project_id || `Project ${index + 1}`)}</option>`).join("");
  }

  function renderAttention() {
    const attention = active.attention_economics && typeof active.attention_economics === "object" ? active.attention_economics : null;
    if (!attention) {
      setAttentionEmpty("Recorded attention is unavailable for this generated snapshot.");
      return;
    }
    const attentionStatus = String(attention.status || "unknown").toLowerCase();
    if (attention.publication_enabled === false || attentionStatus === "disabled") {
      setAttentionEmpty("Attention publication is disabled.");
      return;
    }
    if (["error", "capture_error", "invalid"].includes(attentionStatus)) {
      setAttentionEmpty("Recorded attention is unavailable because the attention source could not be read.");
      return;
    }
    const totals = attention.totals && typeof attention.totals === "object" ? attention.totals : {};
    const recordedAttention = numeric(totals.recorded_attention_hours);
    const dropoffProjects = numeric(totals.dropoff_projects);
    const hasRecordedAttention = attention.has_records !== false && finite(recordedAttention);
    const hasDropoffEvidence = finite(dropoffProjects);
    const retainedAfterSourceError = attentionStatus === "source_error_retained_last_good";
    const coverage = attention.coverage && typeof attention.coverage === "object" ? attention.coverage : {};
    const coverageText = coverage.from && coverage.to ? ` Recorded coverage: ${coverage.from} through ${coverage.to} UTC.` : "";
    const finalizationText = attention.finalization_status === "current_date_pending_utc_close"
      ? " The current UTC date is withheld until it closes."
      : "";
    const comparison = attention.dropoff_comparison && typeof attention.dropoff_comparison === "object" ? attention.dropoff_comparison : {};
    const dropoffPeriod = comparison.from && comparison.to ? `Comparison: ${comparison.from} through ${comparison.to} UTC.` : "";
    $("attention-state").textContent = retainedAfterSourceError
      ? `Last recorded attention retained; the latest attention-source read failed.${coverageText}${finalizationText} Missing timer use is not inferred as zero attention.`
      : hasRecordedAttention
        ? `Only explicitly timed, completed intervals are included.${coverageText}${finalizationText} Missing timer use is not inferred as zero attention.`
        : `No recorded attention in this window.${hasDropoffEvidence ? " The prior-window drop-off comparison remains available." : ""}${coverageText}${finalizationText} Missing timer use is not inferred as zero attention.`;
    $("attention-cards").innerHTML = [
      card("recorded_operator_attention_hours", attentionHours(recordedAttention), "Completed operator-started timer intervals", "", "observed"),
      card("recorded_stewardship_attention_hours", attentionHours(numeric(totals.stewardship_attention_hours)), "Guide + review + rework modes", "", "derived"),
      card("recorded_project_transitions", fmt(numeric(totals.recorded_project_transitions), "number", "no recorded attention"), "Recorded destination changes; no time penalty attached", "", "derived"),
      card("recorded_attention_dropoff_projects", fmt(dropoffProjects, "number", activeKey === "all" ? "not applicable to all-time" : "prior comparison unavailable"), `Previously attended projects with no recorded attention.${dropoffPeriod ? ` ${dropoffPeriod}` : ""}`, "", "derived"),
    ].join("");
    $("attention-secondary").innerHTML = `<span data-metric-id="recorded_rework_attention_hours"><strong>Recorded rework:</strong> ${attentionHours(numeric(totals.rework_attention_hours))} ${metricButton("recorded_rework_attention_hours")} ${evidenceBadge("derived")}</span><span data-metric-id="recorded_rework_share"><strong>Rework share:</strong> ${fmt(numeric(totals.rework_share), "percent", "no recorded attention")} ${metricButton("recorded_rework_share")} ${evidenceBadge("derived")}</span><span data-metric-id="attention_top_project_share"><strong>Top-project attention share:</strong> ${fmt(numeric(totals.top_project_share), "percent", "no recorded attention")} ${metricButton("attention_top_project_share")} ${evidenceBadge("derived")}</span>`;
    $("attention-modes").parentElement.dataset.metricId = "attention_mode_composition";
    $("attention-modes").innerHTML = modeCompositionMarkup(attention.mode_composition);
    const ledger = Array.isArray(attention.project_ledger) ? attention.project_ledger.slice(0, 7) : [];
    $("attention-ledger").parentElement.dataset.metricId = "attention_project_ledger";
    $("attention-ledger").innerHTML = attentionLedgerMarkup(ledger);
    populateScenarioProjects(ledger);
  }

  function scenarioInputValue(id) {
    const value = $(id).value;
    return value.trim() === "" ? null : value;
  }

  function toggleActualCash() {
    const actual = $("scenario-cash-basis").value === "actual_cash";
    $("scenario-actual-field").hidden = !actual;
    $("scenario-actual-cash").disabled = !actual;
    if (!actual) $("scenario-actual-cash").value = "";
  }

  function signedHours(value) {
    const sign = value > 0 ? "+" : value < 0 ? "−" : "";
    return `${sign}${full.format(Math.abs(value))}h`;
  }

  function renderScenario() {
    toggleActualCash();
    const projectIndex = numeric($("scenario-project").value);
    const project = projectIndex === null ? null : scenarioProjects[projectIndex];
    const result = calculateScenario(
      {
        counterfactual_manual_hours:scenarioInputValue("scenario-manual-hours"),
        value_of_attention_usd_per_hour:scenarioInputValue("scenario-value-hour"),
        cash_basis:$("scenario-cash-basis").value,
        actual_cash_usd:scenarioInputValue("scenario-actual-cash"),
        alternative_name:$("scenario-alternative-name").value,
        displaced_share_percent:scenarioInputValue("scenario-displaced-share"),
        alternative_value_usd_per_hour:scenarioInputValue("scenario-alternative-value"),
      },
      project,
    );
    if (!result.valid) {
      renderScenarioEmpty("Complete every required assumption with a valid value to see scenario results. Nothing is stored or sent.");
      return;
    }
    const cashText = result.cashBasis === "none"
      ? "no cash basis"
      : result.cashBasis === "api_equivalent"
        ? `${money.format(result.cashUsd)} exact API-list-price equivalent (not an invoice)`
        : `${money.format(result.cashUsd)} browser-entered actual cash`;
    const deltaMeaning = result.attentionDeltaHours > 0 ? "attention returned" : result.attentionDeltaHours < 0 ? "additional attention required" : "no attention difference";
    $("scenario-result").innerHTML = `<div class="scenario-output"><div class="scenario-output-item" data-metric-id="recorded_operator_attention_hours"><span>Recorded attention</span><strong>${full.format(result.recordedAttentionHours)}h</strong>${evidenceBadge("observed", "Recorded")} ${metricButton("recorded_operator_attention_hours")}</div><div class="scenario-output-item" data-metric-id="scenario_attention_delta_hours"><span>Scenario attention delta</span><strong>${signedHours(result.attentionDeltaHours)}</strong>${evidenceBadge("scenario")} ${metricButton("scenario_attention_delta_hours")}</div><div class="scenario-output-item" data-metric-id="scenario_attention_equivalent_hours"><span>Attention-equivalent total</span><strong>${full.format(result.attentionEquivalentHours)}h</strong>${evidenceBadge("scenario")} ${metricButton("scenario_attention_equivalent_hours")}</div><div class="scenario-output-item" data-metric-id="scenario_opportunity_cost_usd"><span>Scenario opportunity cost</span><strong>${money.format(result.opportunityCostUsd)}</strong>${evidenceBadge("scenario")} ${metricButton("scenario_opportunity_cost_usd")}</div></div><p class="scenario-assumption">Assumes ${full.format(result.recordedAttentionHours)} recorded hours for ${esc(project.label || project.project_id)}, ${cashText}, and that ${full.format(result.displacedSharePercent)}% of recorded attention displaces “${esc(result.alternativeName)}” valued at ${money.format(result.alternativeValueUsdPerHour)}/hour; the signed delta means ${esc(deltaMeaning)}.</p>`;
    updateTestHook();
  }

  function renderScenarioEmpty(message) {
    const items = [
      ["scenario_attention_delta_hours", "Scenario attention delta"],
      ["scenario_attention_equivalent_hours", "Attention-equivalent total"],
      ["scenario_opportunity_cost_usd", "Scenario opportunity cost"],
    ].map(([metricId, label]) => `<div class="scenario-output-item" data-metric-id="${metricId}"><span>${label}</span><strong class="empty">n/a · assumptions incomplete</strong>${evidenceBadge("scenario")} ${metricButton(metricId)}</div>`).join("");
    $("scenario-result").innerHTML = `<p class="scenario-assumption">${esc(message)}</p><div class="scenario-output">${items}</div>`;
    updateTestHook();
  }

  function clearScenario() {
    $("scenario-form").reset();
    toggleActualCash();
    renderScenarioEmpty("Complete every assumption to see scenario results. Nothing is stored or sent.");
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
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable data table"><table><thead><tr>${headers.map(([label, numeric, metricId]) => `<th${numeric ? ' class="num"' : ""}>${esc(label)}${metricId ? ` ${metricButton(metricId)}` : ""}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
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
    $("metric-dialog-body").innerHTML = `<p>${metric.evidence_class ? evidenceBadge(metric.evidence_class) : ""}</p><p>${esc(metric.definition)}</p><div class="formula">${esc(metric.derivation)}</div><p class="catalog-meta"><strong>Unit:</strong> ${esc(metric.unit)}<br><strong>Source:</strong> ${metric.sources.map(esc).join(" · ")}<br><strong>Caveat:</strong> ${esc(metric.caveats)}<br><strong>Catalog id:</strong> ${esc(metric.metric_id)}</p>`;
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
      capacitySignature:JSON.stringify(data.capacity_now || {}),
      capacityProviderState,
      capacityWindowState,
      capacityStateText,
      calculateScenario,
      relativeDuration,
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
    renderAttention();
    renderOutcomes();
    renderReliability();
    renderEvidence();
    $("generated-foot").textContent = `Generated ${when(data.generated_at)} · compact page is within its published budget · full envelope and all machine URLs retained · no runtime network requests.`;
    updateTestHook();
  }

  document.querySelectorAll("[data-window]").forEach(button => button.addEventListener("click", () => {
    activeKey = button.dataset.window;
    clearScenario();
    const url = new URL(window.location.href);
    url.searchParams.delete("from");
    url.searchParams.delete("to");
    url.searchParams.set("window", activeKey);
    window.history.replaceState({}, "", url);
    render();
  }));
  document.querySelectorAll("details.drill").forEach(detail => detail.addEventListener("toggle", () => {
    const body = detail.querySelector("[data-lazy-body]");
    if (body && detail.open && body.dataset.built !== "true") lazyBody(detail.id);
    updateTestHook();
  }));
  document.addEventListener("click", event => {
    const button = event.target.closest && event.target.closest("[data-explain]");
    if (button) showMetric(button.dataset.explain);
  });
  $("metric-dialog-close").addEventListener("click", () => $("metric-dialog").close());
  $("scenario-form").addEventListener("input", renderScenario);
  $("scenario-form").addEventListener("change", renderScenario);
  $("scenario-form").addEventListener("submit", event => event.preventDefault());
  $("scenario-clear").addEventListener("click", clearScenario);
  if (data.payload_kind !== "bounded_page_envelope") {
    document.body.innerHTML = '<main class="shell"><section><h1>Telemetry unavailable</h1><p class="empty">The bounded page envelope is missing or incompatible.</p></section></main>';
    return;
  }
  renderCapacity();
  render();
  renderScenario();
  window.setInterval(() => { renderMasthead(); refreshCapacityPreservingInteraction(); const cardNode = document.querySelector('[data-metric-id="data_age_minutes"] .value'); if (cardNode) cardNode.innerHTML = fmt(ageMinutes(), "minutes"); updateTestHook(); }, 60000);
})();
