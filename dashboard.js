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

  function capacityWindowLabel(windowValue) {
    const value = windowValue || {};
    const minutes = numericValue(value.window_minutes);
    if (Number.isFinite(minutes) && minutes > 0) {
      if (minutes % 1440 === 0) return `${minutes / 1440}-day window`;
      if (minutes % 60 === 0) return `${minutes / 60}-hour window`;
      return `${minutes}-minute window`;
    }
    return value.display_label || value.window || "Reported window";
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

  function sectionVisibility(windowValue) {
    const value = windowValue || {};
    const attention = value.attention_economics || {};
    const totals = attention.totals || {};
    const blocked = attention.publication_enabled === false
      || ["disabled", "error", "capture_error", "invalid"].includes(attention.status);
    const hasRecords = attention.has_records !== false
      && Number.isFinite(numericValue(totals.recorded_attention_hours));
    const hasDropoff = numericValue(totals.dropoff_projects) > 0;
    const report = value.investment_results || {};
    return {
      attention:!blocked && (hasRecords || hasDropoff),
      results:numericValue((report.totals || {}).results?.outcomes) > 0
        || numericValue((report.totals || {}).code?.revisions) > 0,
      loop:numericValue((value.outcomes || {}).rounds) > 0
        || (Array.isArray(value.top_specs) && value.top_specs.length > 0),
    };
  }

  function displayAliases(windowValues, family = "projects", previous = {}) {
    const aliases = Object.create(null);
    const identities = new Set();
    ["7", "30", "90", "all"].forEach(windowKey => {
      const value = (windowValues || {})[windowKey];
      if (!value) return;
      const rows = family === "features"
        ? [...(value.top_specs || []).slice(0, 7), ...(value.recent_specs || []).slice(0, 6).map(row => ({label:row.spec}))]
        : [...(value.top_projects || []).slice(0, 7), ...(value.attention_economics?.project_ledger || []).slice(0, 7), ...(value.investment_results?.projects || []).slice(0, 7)];
      rows.forEach(row => {
        const key = String(row.project_id || row.label || "");
        if (key && !row.other_count && !["other", "ad-hoc", "remote", "Shared/Unassigned"].includes(key)) identities.add(key);
      });
    });
    let next = Math.max(0, ...Object.values(previous)) + 1;
    [...identities].sort().forEach(key => {
      aliases[key] = Object.prototype.hasOwnProperty.call(previous, key) ? previous[key] : next++;
    });
    return aliases;
  }

  function displayAlias(row, aliases, family = "projects") {
    const key = String(row.project_id || row.label || "");
    if (row.other_count || key === "other") return "Other";
    if (family === "projects" && key === "ad-hoc") return "Ad hoc";
    if (family === "projects" && key === "remote") return "Remote";
    if (family === "projects" && key === "Shared/Unassigned") return "Shared / unassigned";
    const prefix = family === "features" ? "Feature" : "Project";
    return aliases[key] ? `${prefix} ${String(aliases[key]).padStart(2, "0")}` : prefix;
  }

  function chartScale(values) {
    const highest = Math.max(0, ...values.filter(finiteNumber));
    if (!highest) return 1;
    const power = 10 ** Math.floor(Math.log10(highest));
    const normalized = highest / power;
    const ceiling = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10) * power;
    return Number.isFinite(ceiling) ? ceiling : highest;
  }

  function trendSegments(rows, key) {
    const segments = [];
    let current = [];
    rows.forEach((row, index) => {
      const value = row[key];
      if (finiteNumber(value)) {
        current.push({index, value});
      } else if (current.length) {
        segments.push(current);
        current = [];
      }
    });
    if (current.length) segments.push(current);
    return segments;
  }

  function snapshotDecision(currentValue, candidateValue) {
    const current = currentValue && typeof currentValue === "object" ? currentValue : {};
    const candidate = candidateValue && typeof candidateValue === "object" ? candidateValue : {};
    const currentCatalog = Array.isArray(current.catalog) ? current.catalog : [];
    const candidateCatalog = Array.isArray(candidate.catalog) ? candidate.catalog : [];
    const currentIds = currentCatalog.map(row => row && row.metric_id).filter(value => typeof value === "string");
    const candidateIds = candidateCatalog.map(row => row && row.metric_id).filter(value => typeof value === "string");
    const currentIdSet = new Set(currentIds);
    const candidateIdSet = new Set(candidateIds);
    const candidateCatalogById = new Map(candidateCatalog.map(row => [row && row.metric_id, row]));
    const currentKeys = current.contract && Array.isArray(current.contract.window_keys) ? current.contract.window_keys : [];
    const candidateKeys = candidate.contract && Array.isArray(candidate.contract.window_keys) ? candidate.contract.window_keys : [];
    const compatibleShape = (reference, value, allowNewNull = true) => {
      if (Array.isArray(reference)) return Array.isArray(value);
      if (reference && typeof reference === "object") {
        if (!value || typeof value !== "object" || Array.isArray(value)) return false;
        return Object.keys(reference).every(key => Object.prototype.hasOwnProperty.call(value, key) && compatibleShape(reference[key], value[key], allowNewNull));
      }
      return reference === null || (allowNewNull && value === null) || typeof value === typeof reference;
    };
    const valid = candidate.payload_kind === "bounded_page_envelope"
      && candidate.schema_version === current.schema_version
      && compatibleShape(current.contract, candidate.contract, false)
      && compatibleShape(current.point_in_time, candidate.point_in_time)
      && candidate.windows && typeof candidate.windows === "object" && !Array.isArray(candidate.windows)
      && candidateCatalog.length >= currentCatalog.length
      && candidateIds.length === candidateCatalog.length
      && candidateIdSet.size === candidateIds.length
      && currentIds.length === currentCatalog.length
      && currentIdSet.size === currentIds.length
      && currentCatalog.every(row => candidateIdSet.has(row.metric_id) && compatibleShape(row, candidateCatalogById.get(row.metric_id), false))
      && currentKeys.length > 0
      && currentKeys.every(key => candidateKeys.includes(key) && compatibleShape(current.windows[key], candidate.windows[key]));
    const candidateGenerated = parsedMillis(candidate.generated_at);
    if (!valid || candidateGenerated === null) return "invalid";
    const currentGenerated = parsedMillis(current.generated_at);
    if (currentGenerated === null || candidateGenerated > currentGenerated) return "newer";
    if (candidateGenerated === currentGenerated) return "unchanged";
    return "older";
  }

  function telemetryRefreshUrl(baseURI, nowMillis, intervalMinutes = 1) {
    const interval = numericValue(intervalMinutes);
    if (!finiteNumber(nowMillis) || !Number.isFinite(interval) || interval <= 0) return null;
    try {
      const url = new URL("data/telemetry.js", baseURI);
      url.searchParams.set("refresh", String(Math.floor(nowMillis / (interval * 60000))));
      return url.href;
    } catch (_error) {
      return null;
    }
  }

  function snapshotRefreshSlot(nowMillis, intervalMinutes = 1, offsetMinutes = 0) {
    const interval = numericValue(intervalMinutes);
    const offset = numericValue(offsetMinutes);
    if (!finiteNumber(nowMillis) || !Number.isFinite(interval) || interval <= 0 || !Number.isFinite(offset) || offset < 0 || offset >= interval) return null;
    return Math.floor((nowMillis - offset * 60000) / (interval * 60000));
  }

  function nextSnapshotRefreshMillis(nowMillis, intervalMinutes = 1, offsetMinutes = 0) {
    const slot = snapshotRefreshSlot(nowMillis, intervalMinutes, offsetMinutes);
    if (slot === null) return null;
    return (slot + 1) * intervalMinutes * 60000 + offsetMinutes * 60000;
  }

  return Object.freeze({capacityProviderState, capacityWindowState, capacityWindowLabel, captureStatusFailed, calculateScenario, relativeDuration, sectionVisibility, displayAliases, displayAlias, chartScale, trendSegments, snapshotDecision, telemetryRefreshUrl, snapshotRefreshSlot, nextSnapshotRefreshMillis});
})();

if (typeof window !== "undefined") window.AgentTelemetryUI = AgentTelemetryUI;
if (typeof module === "object" && module.exports) module.exports = AgentTelemetryUI;

(() => {
  "use strict";

  if (typeof window === "undefined" || typeof document === "undefined") return;

  let data = window.TELEMETRY || {};
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const numeric = value => typeof value === "number" && Number.isFinite(value) ? value : typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
  const sum = values => values.reduce((total, value) => total + (finite(value) ? value : 0), 0);
  const compact = new Intl.NumberFormat("en-US", {maximumFractionDigits:1, notation:"compact"});
  const axisCompact = new Intl.NumberFormat("en-US", {maximumFractionDigits:2, notation:"compact"});
  const full = new Intl.NumberFormat("en-US", {maximumFractionDigits:2});
  const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:2});
  const percentNumber = new Intl.NumberFormat("en-US", {maximumFractionDigits:1});
  const colors = ["#7bdcff", "#8ba9ff", "#75e6ad", "#ffd166", "#c4a7ff", "#ff8c9b", "#b8c5d3"];
  const {capacityProviderState, capacityWindowState, capacityWindowLabel, relativeDuration, sectionVisibility, displayAliases, displayAlias, chartScale, trendSegments, snapshotDecision, telemetryRefreshUrl, snapshotRefreshSlot, nextSnapshotRefreshMillis} = AgentTelemetryUI;
  const focusableSelector = "button,summary,a[href],input,select,textarea,[tabindex]:not([tabindex='-1'])";
  const snapshotRefreshIntervalMinutes = 1;
  const snapshotRefreshOffsetMinutes = 0;
  const snapshotRefreshIntervalMs = snapshotRefreshIntervalMinutes * 60000;
  let catalog = new Map((data.catalog || []).map(row => [row.metric_id, row]));
  let windows = data.windows || {};
  let validWindows = data.contract && data.contract.window_keys || ["7", "30", "90", "all"];
  const params = new URLSearchParams(window.location.search);
  let activeKey = validWindows.includes(params.get("window")) ? params.get("window") : data.default_window || "30";
  let active = windows[activeKey] || Object.values(windows)[0] || {};
  let projectAliases = displayAliases(windows);
  let featureAliases = displayAliases(windows, "features");
  let snapshotRefreshTimer = null;
  let snapshotRefreshInFlight = false;
  let snapshotRefreshLastSlot = snapshotRefreshSlot(Date.now(), snapshotRefreshIntervalMinutes, snapshotRefreshOffsetMinutes);
  let snapshotRefreshState = "scheduled";
  let snapshotRefreshCheckedAt = null;
  let metricReturnFocus = null;

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

  function card(metricId, value, detail = "", delta = "") {
    const metric = catalog.get(metricId) || {display_label:metricId};
    if (value.includes('class="empty"')) return "";
    return `<article class="card" data-metric-id="${esc(metricId)}"><div class="metric-head"><span class="label">${esc(metric.display_label)}</span>${metricButton(metricId)}</div><span class="value">${value}</span>${detail ? `<span class="detail">${detail}</span>` : ""}${delta ? `<span class="delta" title="Change from the preceding equal UTC window" aria-label="Change from the preceding equal UTC window: ${esc(delta)}">${delta}</span>` : ""}</article>`;
  }

  function deltaText(current, previous, kind = "number") {
    if (!finite(current) || !finite(previous)) return "";
    const delta = current - previous;
    const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "↔";
    const magnitude = Math.abs(delta);
    const value = kind === "money" ? money.format(magnitude) : kind === "percent" ? `${full.format(magnitude * 100)} pp` : kind === "tokens" ? compact.format(magnitude) : kind === "minutes" ? `${full.format(magnitude)}m` : full.format(magnitude);
    return `${arrow} ${delta > 0 ? "+" : delta < 0 ? "−" : ""}${value}`;
  }

  function chartHeader(metricId, subtitle) {
    const metric = catalog.get(metricId) || {display_label:metricId};
    return `<div class="chart-title"><div><h3>${esc(metric.display_label)}</h3><span>${esc(subtitle)}</span></div>${metricButton(metricId)}</div>`;
  }

  function donut(targetId, metricId, rows, valueKey, subtitle, formatter = value => fmt(value, "tokens")) {
    const target = $(targetId);
    const clean = rows.filter(row => finite(row[valueKey]) && row[valueKey] >= 0);
    target.hidden = !clean.length;
    if (target.hidden) { target.replaceChildren(); return; }
    const total = sum(clean.map(row => Number(row[valueKey])));
    const circumference = 2 * Math.PI * 44;
    let offset = 0;
    const circles = clean.map((row, index) => {
      const length = total ? Number(row[valueKey]) / total * circumference : 0;
      const circle = `<circle cx="50" cy="50" r="44" stroke="${colors[index % colors.length]}" stroke-dasharray="${length} ${circumference}" stroke-dashoffset="${-offset}"><title>${esc(row.label)}: ${esc(formatter(row[valueKey]))}</title></circle>`;
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
    rows = rows.slice(0, 48);
    target.hidden = !rows.some(row => series.some(item => finite(row[item.key])));
    if (target.hidden) { target.replaceChildren(); return; }
    const maximum = chartScale(rows.flatMap(row => series.map(item => row[item.key])));
    const x = index => rows.length <= 1 ? 50 : 4 + index / (rows.length - 1) * 92;
    const y = value => 92 - value / maximum * 84;
    const dateLabel = row => row.from && row.to && row.from !== row.to ? `${row.from} → ${row.to}` : row.from || row.date;
    const format = series[0].axisFormatter || series[0].formatter || (value => full.format(value));
    const polylines = series.map((item, index) => {
      const color = item.color || colors[index];
      const formatPoint = item.formatter || format;
      return trendSegments(rows, item.key).map(segment => {
        const points = segment.map(point => `${x(point.index).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
        const firstX = x(segment[0].index).toFixed(2);
        const lastX = x(segment[segment.length - 1].index).toFixed(2);
        const firstIndex = rows.findIndex(row => finite(row[item.key]));
        const markers = segment.map(point => {
          const label = `${dateLabel(rows[point.index])} · ${item.label}: ${formatPoint(point.value)}`;
          return `<circle class="plot-point${segment.length === 1 ? " single-point" : ""}" cx="${x(point.index).toFixed(2)}" cy="${y(point.value).toFixed(2)}" r="1.5" stroke="${color}" tabindex="${point.index === firstIndex ? 0 : -1}" role="img" aria-label="${esc(label)}" data-point-label="${esc(label)}" data-series="${index}" data-bucket="${point.index}"><title>${esc(label)}</title></circle>`;
        }).join("");
        return `<polygon class="area" points="${firstX},92 ${points} ${lastX},92" fill="${color}"></polygon><polyline points="${points}" stroke="${color}"></polyline>${markers}`;
      }).join("");
    }).join("");
    const first = rows[0];
    const last = rows[rows.length - 1];
    const labels = `<div class="plot-labels"><span>${esc(first.from || first.date)}</span><span>${esc(last.to || last.date)}</span></div>`;
    const key = series.map((item, index) => `<span><b style="color:${item.color || colors[index]}">— ${esc(item.label)}</b></span>`).join("");
    const grid = [8, 50, 92].map(position => `<line class="grid" x1="4" y1="${position}" x2="96" y2="${position}"></line>`).join("");
    const scale = [maximum, maximum / 2, 0].map(value => `<span>${esc(format(value))}</span>`).join("");
    target.dataset.metricId = metricId;
    target.innerHTML = `${chartHeader(metricId, subtitle)}<div class="chart-body"><div class="plot-wrap"><div class="plot-scale" aria-hidden="true">${scale}</div><div><svg class="plot" viewBox="0 0 100 100" preserveAspectRatio="none" role="group" aria-label="${esc(subtitle)}" aria-description="Use left and right arrow keys to inspect values, or Home and End for the first and last bucket.">${grid}${polylines}</svg>${labels}</div></div><div class="series-key">${key}</div><div class="chart-tooltip" aria-live="polite"></div></div>`;
  }

  function ranked(targetId, metricId, rows, valueKey, subtitle, formatter, color = colors[0]) {
    const target = $(targetId);
    rows = rows.slice(0, 7);
    target.hidden = !rows.some(row => finite(row[valueKey]));
    if (target.hidden) { target.replaceChildren(); return; }
    const maximum = Math.max(1, ...rows.map(row => Number(row[valueKey]) || 0));
    const body = rows.map(row => {
      const label = row.other_count ? "Other" : row.label;
      const other = row.other_count || label === "Other";
      return `<div class="rank-row${other ? " other-row" : ""}"><span class="rank-name" title="${esc(label)}">${esc(label)}</span><span class="track" aria-hidden="true"><span class="fill" style="width:${(Number(row[valueKey]) || 0) / maximum * 100}%;background:${other ? colors[6] : color}"></span></span><span class="rank-value">${formatter(Number(row[valueKey]) || 0)}</span></div>`;
    }).join("");
    target.dataset.metricId = metricId;
    target.innerHTML = `${chartHeader(metricId, subtitle)}<div class="chart-body ranked">${body}</div>`;
  }

  function status(targetId, ok, goodText, badText, warning = false) {
    const target = $(targetId);
    target.className = `status ${ok ? (warning ? "warn" : "good") : "bad"}`;
    target.textContent = ok ? goodText : badText;
  }

  function renderMasthead() {
    const refreshText = snapshotRefreshState === "updated"
      ? "Snapshot updated automatically"
      : snapshotRefreshState === "failed-last-good"
        ? "Auto-check retrying · last-good retained"
        : "";
    const relative = relativeDuration(data.generated_at);
    $("mast-meta").innerHTML = `<span data-metric-id="data_age_minutes">Updated ${relative ? `${esc(relative.text)} ago` : "at an unknown time"} ${metricButton("data_age_minutes")}</span>${snapshotRefreshState === "failed-last-good" ? `<span class="refresh-note">${esc(refreshText)}</span>` : ""}`;
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
    const remaining = evaluated.hasValue ? `${percentNumber.format(evaluated.remainingPercent)}% left` : "Unknown";
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
      ? `<progress max="100" value="${evaluated.remainingPercent}" aria-label="${esc(capacityWindowLabel(windowValue))}: ${esc(remaining)}; ${esc(evaluated.state.replace(/_/g, " "))}"></progress>`
      : "";
    const observedTime = windowValue.observed_at && Number.isFinite(Date.parse(windowValue.observed_at))
      ? ` · ${timeMarkup(windowValue.observed_at)}`
      : "";
    const stateLabel = {available:"Fresh", stale:"Stale · last reported", retained_last_good:"Last reported · capture failed", error:"Capture error", unavailable:"Unavailable"}[evaluated.state];
    const reset = relativeDuration(windowValue.resets_at);
    const resetBrief = reset ? `${reset.direction === "future" ? "Resets in" : "Reset passed"} ${reset.text}` : "Reset not reported";
    return `<article class="capacity-window" data-capacity-state="${esc(evaluated.state)}" data-metric-id="${metricId}"><div class="capacity-window-head"><div><h3>${esc(capacityWindowLabel(windowValue))}</h3></div>${metricButton(metricId)}</div><div class="capacity-remaining">${esc(remaining)}</div><span class="capacity-state" data-state="${esc(evaluated.state)}">${esc(stateLabel)}</span>${progress}<div class="capacity-times"><span>${esc(relativeObservation(windowValue.observed_at, windowValue.age_hours))}</span><span>${esc(resetBrief)}</span></div><details class="capacity-source"><summary>Source and capture</summary><p>${evidenceBadge("observed")} ${esc(used)}${esc(windowMinutes)}. ${esc(windowValue.display_label || windowValue.window || `Window ${index + 1}`)}.</p><p>${capacityStateText(evaluated.state, windowValue)}${observedTime}<br>${resetDescription(windowValue.resets_at, windowValue.observed_at)}</p><p>Source: ${esc(windowValue.source || "not reported")} · capture: ${esc(windowValue.capture_status || "not reported")} · freshness at generation: ${esc(windowValue.freshness_status || "not reported")}. This is provider-reported capacity, not billing or an estimate of messages remaining.</p></details></article>`;
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

  function providerEmoji(provider) {
    const identity = `${provider.provider || ""} ${provider.display_label || ""}`.toLowerCase();
    if (/(anthropic|claude)/.test(identity)) return "🟠";
    if (/(openai|codex)/.test(identity)) return "🟢";
    return "⚪";
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
      const additionalText = additional > 0 ? `${full.format(additional)} additional reported window${additional === 1 ? "" : "s"} omitted from this bounded page.` : "";
      return `<article class="capacity-provider"><div class="capacity-provider-head"><div><h3 class="provider-heading"><span class="provider-emoji" aria-hidden="true">${providerEmoji(provider)}</span><span>${esc(provider.display_label || provider.provider || "Provider")}</span></h3><span class="detail">${esc(additionalText)}</span></div></div><div class="capacity-windows">${windowMarkup}</div></article>`;
    });
    $("capacity-providers").innerHTML = rows.join("") || '<p class="empty">Provider capacity is unavailable for this generated snapshot.</p>';
    const check = $("capacity-check");
    const bad = renderedStates.includes("error");
    const warning = renderedStates.some(value => ["stale", "retained_last_good", "unavailable"].includes(value)) || !renderedStates.length;
    check.className = `status ${bad ? "bad" : warning ? "warn" : "good"}`;
    check.textContent = bad ? "Capture error" : warning ? "Capacity partial" : "Capacity fresh";
    $("capacity-update").textContent = "Refreshes automatically · provider observation ages shown above.";
  }

  function refreshCapacityPreservingInteraction() {
    const capacityRoot = $("capacity-now");
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
      card("lifetime_tokens", fmt(totals.tokens, "tokens")),
      card("lifetime_cost_usd", fmt(totals.cost_usd, "money")),
      card("lifetime_unpriced_tokens", fmt(totals.unpriced_tokens, "tokens")),
      card("lifetime_sessions", fmt(totals.sessions)),
    ].join("");
    donut("vendor-chart", "tokens_by_vendor", point.by_vendor || [], "tokens", "All-time");
    donut("host-chart", "tokens_by_host_os", point.by_host_os || [], "tokens", "All-time");
    status("overview-check", point.reconciliation === "ok" && point.store_integrity === "ok", "Reconciled", "Totals need attention");
  }

  function renderActivity() {
    const summary = active.summary || {};
    const prior = (active.comparison || {}).summary || {};
    $("activity-cards").innerHTML = [
      card("window_tokens", fmt(summary.tokens, "tokens"), "", deltaText(summary.tokens, prior.tokens, "tokens")),
      card("window_cost_usd", fmt(summary.cost_usd, "money"), summary.unpriced_tokens > 0 ? `${fmt(summary.unpriced_tokens, "tokens")} tokens unpriced` : "", deltaText(summary.cost_usd, prior.cost_usd, "money")),
      card("window_session_days", fmt(summary.session_days), "", deltaText(summary.session_days, prior.session_days)),
      card("window_active_days", fmt(summary.active_days), "", deltaText(summary.active_days, prior.active_days)),
    ].join("");
    lineChart("token-trend", "daily_tokens", active.daily || [], [{key:"tokens", label:"tokens per bucket", color:colors[0], formatter:value => full.format(value), axisFormatter:value => axisCompact.format(value)}], "UTC bucket totals");
    lineChart("cost-trend", "daily_cost_usd", active.daily || [], [{key:"cost_usd", label:"exact USD per bucket", color:colors[1], formatter:value => money.format(value), axisFormatter:value => `$${axisCompact.format(value)}`}], "API-equivalent USD · UTC bucket totals");
  }

  function renderMix() {
    const point = data.point_in_time || {};
    const rows = (active.top_projects || []).map(row => ({...row, label:displayAlias(row, projectAliases)}));
    ranked("project-chart", "tokens_by_project", rows, "tokens", "Selected window · top six + Other", value => fmt(value, "tokens"));
    ranked("model-chart", "tokens_by_model", point.top_models || [], "tokens", "All-time · top six + Other", value => fmt(value, "tokens"), colors[1]);
  }

  function attentionHours(value) {
    return finite(value) ? `${full.format(value)}h` : '<span class="empty">n/a</span>';
  }

  function modeCompositionMarkup(rows) {
    return (Array.isArray(rows) ? rows : []).filter(row => finite(row.seconds)).slice(0, 5).map(row => {
      const share = numeric(row.share);
      const width = share === null ? 0 : Math.max(0, Math.min(1, share)) * 100;
      return `<div class="mode-row"><span class="mode-name">${esc(row.mode)}</span><span class="mode-track" aria-hidden="true"><span class="mode-fill" style="width:${width}%"></span></span><span class="mode-value">${full.format(row.seconds / 3600)}h${share === null ? "" : ` · ${percentNumber.format(share * 100)}%`}</span></div>`;
    }).join("");
  }

  function renderAttention() {
    const attention = active.attention_economics || {};
    const totals = attention.totals || {};
    if (!sectionVisibility(active).attention) {
      $("attention-cards").replaceChildren();
      $("attention-modes").replaceChildren();
      $("attention-projects").replaceChildren();
      return;
    }
    const comparison = attention.dropoff_comparison || {};
    let closedTo = active.to;
    if (attention.finalization_status === "current_date_pending_utc_close") {
      const date = new Date(`${active.to}T00:00:00Z`);
      if (Number.isFinite(date.valueOf())) {
        date.setUTCDate(date.getUTCDate() - 1);
        closedTo = date.toISOString().slice(0, 10);
      }
    }
    $("attention-state").textContent = `${active.from} → ${closedTo} UTC${attention.status === "source_error_retained_last_good" ? " · source unavailable; last recorded values" : ""}`;
    $("attention-cards").innerHTML = [
      card("recorded_operator_attention_hours", attentionHours(numeric(totals.recorded_attention_hours))),
      card("recorded_stewardship_attention_hours", attentionHours(numeric(totals.stewardship_attention_hours))),
      card("recorded_project_transitions", fmt(numeric(totals.recorded_project_transitions))),
      card("recorded_attention_dropoff_projects", fmt(numeric(totals.dropoff_projects)), comparison.from && comparison.to ? `${esc(comparison.from)} → ${esc(comparison.to)} UTC` : ""),
    ].join("");
    $("attention-modes").innerHTML = modeCompositionMarkup(attention.mode_composition);
    $("attention-modes").closest(".chart-card").hidden = !$("attention-modes").children.length;
    const rows = (attention.project_ledger || []).filter(row => finite(row.recorded_attention_hours)).slice(0, 7)
      .map(row => ({...row, label:displayAlias(row, projectAliases)}));
    ranked("attention-projects", "attention_project_ledger", rows, "recorded_attention_hours", "Recorded hours · top six + Other", attentionHours, colors[2]);
  }

  function renderEconomics() {
    if (!sectionVisibility(active).results) {
      $("economics-cards").replaceChildren();
      return;
    }
    const report = active.investment_results || {};
    const totals = report.totals || {};
    const results = totals.results || {};
    const period = report.period || {};
    $("economics-period").textContent = `${period.from} → ${period.to} · closed UTC dates`;
    $("economics-cards").innerHTML = [
      card("economics_delivered", fmt(numeric(results.delivered))),
      numeric(results.reviewed) > 0 ? card("economics_accepted", fmt(numeric(results.human_accepted))) : "",
      numeric(results.checks_passed) + numeric(results.checks_failed) > 0 ? card("economics_verified", fmt(numeric(results.verified_delivered))) : "",
      card("economics_code_changes", fmt(numeric((totals.code || {}).revisions))),
    ].join("");
  }

  function loopHistoryNote() {
    const history = (data.point_in_time || {}).loop_history || {};
    if (history.status !== "historical") return "";
    const collected = typeof history.last_collected_at === "string" && history.last_collected_at.length >= 10 ? history.last_collected_at.slice(0, 10) : "unknown";
    return `Historical · retired ${history.retired_on || "2026-09-08"} · last collected ${collected}`;
  }

  function renderLoopHistoryNotes() {
    $("outcomes-note").textContent = loopHistoryNote() || "Historical · loop retired 2026-09-08";
  }

  function renderOutcomes() {
    const outcome = active.outcomes || {};
    const prior = (active.comparison || {}).outcomes || {};
    renderLoopHistoryNotes();
    if (!sectionVisibility(active).loop) {
      ["outcome-cards", "round-outcome-chart", "spec-cost-chart"].forEach(id => $(id).replaceChildren());
      return;
    }
    $("outcome-cards").innerHTML = [
      card("accepted_features", fmt(outcome.accepted_features), "", deltaText(outcome.accepted_features, prior.accepted_features)),
      card("acceptance_efficiency", fmt(outcome.acceptance_efficiency, "percent", "no specs in window"), "", deltaText(outcome.acceptance_efficiency, prior.acceptance_efficiency, "percent")),
      card("mean_cost_per_accepted", fmt(outcome.mean_cost_per_accepted, "money", "no accepted feature in window"), "", deltaText(outcome.mean_cost_per_accepted, prior.mean_cost_per_accepted, "money")),
      card("median_round_minutes", fmt(outcome.median_round_minutes, "minutes", "no complete rounds"), "", deltaText(outcome.median_round_minutes, prior.median_round_minutes, "minutes")),
    ].join("");
    lineChart("round-outcome-chart", "rounds_by_day", active.rounds_by_day || [], [
      {key:"accepted", label:"accepted rounds", color:colors[2]},
      {key:"not_accepted", label:"non-accepted rounds", color:colors[3]},
    ], "UTC completion buckets");
    ranked("spec-cost-chart", "spec_cost_rank", (active.top_specs || []).map(row => ({...row, label:displayAlias(row, featureAliases, "features")})), "cost_usd", "Selected window · top six + Other", value => fmt(value, "money"), colors[2]);

  }

  function renderReliability() {
    const point = data.point_in_time || {};
    const doctor = point.doctor || {};
    const known = ["ok", "warn", "fail"].includes(doctor.status);
    status("reliability-check", doctor.status !== "fail", doctor.status === "ok" ? "Healthy" : known ? "Warning" : "Unknown", "Failed", doctor.status !== "ok");
    refreshLazy("health-detail");
  }

  function table(headers, rows) {
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable data table"><table><thead><tr>${headers.map(([label, numeric, metricId]) => `<th${numeric ? ' class="num"' : ""}>${esc(label)}${metricId ? ` ${metricButton(metricId)}` : ""}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }

  function lazyBody(detailId) {
    if (detailId !== "health-detail") return;
    const point = data.point_in_time || {};
    const body = $(detailId).querySelector("[data-lazy-body]");
    $("reliability-cards").innerHTML = [
      card("missed_intervals", fmt((point.cadence || {}).missed_intervals)),
      card("disk_runway_years", fmt((point.disk || {}).runway_years, "years")),
    ].join("");
    const checks = ((point.doctor || {}).checks || []).slice(0, 48).map(row => `<tr><td>${esc(row.name)}</td><td>${esc(row.status)}</td></tr>`);
    const roots = (point.roots || []).slice(0, 4).map(row => `<tr><td>${esc(row.root_id)}</td><td>${esc(row.status)}</td><td>${esc(when(row.last_success_at))}</td></tr>`);
    body.innerHTML = `<h3>Doctor checks</h3>${table([["Check",false],["Status",false,"doctor_status"]], checks)}<h3 style="margin-top:14px">Provider roots</h3>${table([["Root",false],["Status",false,"source_root_status"],["Last successful scan",false,"source_root_status"]], roots)}`;
    body.dataset.built = "true";
  }

  function refreshLazy(detailId) {
    const detail = $(detailId);
    const body = detail.querySelector("[data-lazy-body]");
    $("reliability-cards").replaceChildren();
    body.replaceChildren();
    body.dataset.built = "false";
    if (detail.open) lazyBody(detailId);
  }

  function captureFocusState() {
    const element = document.activeElement;
    if (!element || element === document.body) return null;
    const container = element.closest && element.closest("[id]");
    if (!container) return {element, containerId:null, index:-1};
    const focusables = [...container.querySelectorAll(focusableSelector)];
    return {element, containerId:container.id, index:focusables.indexOf(element), series:element.dataset.series, bucket:element.dataset.bucket};
  }

  function restoreFocusState(state) {
    if (!state || (state.element && state.element.isConnected)) return;
    const container = state.containerId ? $(state.containerId) : null;
    if (!container || state.index < 0) return;
    if (state.series !== undefined && state.bucket !== undefined) {
      const point = [...container.querySelectorAll("[data-point-label]")].find(node => node.dataset.series === state.series && node.dataset.bucket === state.bucket);
      if (point) {
        container.querySelectorAll(`[data-series="${state.series}"]`).forEach(node => { node.tabIndex = -1; });
        point.tabIndex = 0;
        point.focus({preventScroll:true});
        return;
      }
    }
    const focusables = [...container.querySelectorAll(focusableSelector)];
    if (focusables[state.index]) focusables[state.index].focus({preventScroll:true});
  }

  function bindSnapshot(snapshot) {
    data = snapshot;
    window.TELEMETRY = data;
    catalog = new Map((Array.isArray(data.catalog) ? data.catalog : []).map(row => [row.metric_id, row]));
    windows = data.windows && typeof data.windows === "object" ? data.windows : {};
    const contractKeys = data.contract && Array.isArray(data.contract.window_keys) ? data.contract.window_keys : [];
    validWindows = contractKeys.length ? contractKeys : ["7", "30", "90", "all"];
    if (!validWindows.includes(activeKey) || !windows[activeKey]) {
      activeKey = validWindows.includes(data.default_window) && windows[data.default_window]
        ? data.default_window
        : validWindows.find(key => windows[key]) || Object.keys(windows)[0] || "30";
      const url = new URL(window.location.href);
      url.searchParams.set("window", activeKey);
      window.history.replaceState({}, "", url);
    }
    active = windows[activeKey] || Object.values(windows)[0] || {};
    projectAliases = displayAliases(windows, "projects", projectAliases);
    featureAliases = displayAliases(windows, "features", featureAliases);
  }

  function adoptSnapshot(candidate) {
    if (snapshotDecision(data, candidate) !== "newer") return false;
    const focusState = captureFocusState();
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const previous = {data, catalog, windows, validWindows, activeKey, active, projectAliases, featureAliases};
    try {
      bindSnapshot(candidate);
      snapshotRefreshState = "updated";
      refreshCapacityPreservingInteraction();
      render();
      window.scrollTo(scrollX, scrollY);
      restoreFocusState(focusState);
      return true;
    } catch (_error) {
      ({data, catalog, windows, validWindows, activeKey, active, projectAliases, featureAliases} = previous);
      window.TELEMETRY = data;
      snapshotRefreshState = "failed-last-good";
      refreshCapacityPreservingInteraction();
      render();
      window.scrollTo(scrollX, scrollY);
      restoreFocusState(focusState);
      return false;
    }
  }

  function finishSnapshotCheck(script, state) {
    script.remove();
    snapshotRefreshInFlight = false;
    snapshotRefreshState = state;
    snapshotRefreshCheckedAt = new Date().toISOString();
    window.TELEMETRY = data;
    renderMasthead();
    updateTestHook();
  }

  function checkForNewSnapshot(force = false) {
    const now = Date.now();
    const slot = snapshotRefreshSlot(now, snapshotRefreshIntervalMinutes, snapshotRefreshOffsetMinutes);
    if (snapshotRefreshInFlight) return false;
    if (!force && (document.visibilityState === "hidden" || slot === null || slot <= snapshotRefreshLastSlot)) return false;
    const source = telemetryRefreshUrl(document.baseURI, now, snapshotRefreshIntervalMinutes);
    if (!source) {
      snapshotRefreshState = "failed-last-good";
      snapshotRefreshCheckedAt = new Date(now).toISOString();
      renderMasthead();
      updateTestHook();
      return false;
    }
    snapshotRefreshInFlight = true;
    if (slot !== null) snapshotRefreshLastSlot = slot;
    snapshotRefreshState = "checking";
    updateTestHook();
    const script = document.createElement("script");
    script.async = true;
    script.dataset.telemetryRefresh = "true";
    script.src = source;
    let settled = false;
    let timeout = null;
    const settle = state => {
      if (settled) return;
      settled = true;
      if (timeout !== null) window.clearTimeout(timeout);
      finishSnapshotCheck(script, state);
    };
    script.addEventListener("load", () => {
      if (settled) {
        window.TELEMETRY = data;
        return;
      }
      const candidate = window.TELEMETRY;
      const decision = snapshotDecision(data, candidate);
      if (decision === "newer") {
        settle(adoptSnapshot(candidate) ? "updated" : "failed-last-good");
        return;
      }
      settle(decision === "invalid" ? "failed-last-good" : "current");
    }, {once:true});
    script.addEventListener("error", () => settle("failed-last-good"), {once:true});
    timeout = window.setTimeout(() => settle("failed-last-good"), 15000);
    document.head.append(script);
    return true;
  }

  function scheduleSnapshotRefresh() {
    if (snapshotRefreshTimer !== null) window.clearTimeout(snapshotRefreshTimer);
    const now = Date.now();
    const next = nextSnapshotRefreshMillis(now, snapshotRefreshIntervalMinutes, snapshotRefreshOffsetMinutes);
    const delay = Math.max(1000, (next === null ? now + snapshotRefreshIntervalMs : next) - now + 1000);
    snapshotRefreshTimer = window.setTimeout(() => {
      checkForNewSnapshot();
      scheduleSnapshotRefresh();
    }, delay);
  }

  function refreshClientTime() {
    renderMasthead();
    refreshCapacityPreservingInteraction();
    updateTestHook();
  }

  function showMetric(metricId) {
    const metric = catalog.get(metricId);
    if (!metric) return;
    metricReturnFocus = captureFocusState();
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
        openDrilldowns: document.querySelectorAll("details.health-disclosure[open]").length,
        materializedRows: document.querySelectorAll("details.health-disclosure tbody tr").length,
        trendPolylines: document.querySelectorAll(".plot polyline").length,
        rankRows: document.querySelectorAll(".rank-row").length,
      },
      payloadBytes: data.contract && data.contract.payload_bytes,
      capacitySignature:JSON.stringify(data.capacity_now || {}),
      snapshotRefresh: {
        status:snapshotRefreshState,
        checkedAt:snapshotRefreshCheckedAt,
        inFlight:snapshotRefreshInFlight,
        intervalMinutes:snapshotRefreshIntervalMinutes,
        offsetMinutes:snapshotRefreshOffsetMinutes,
        generatedAt:data.generated_at || null,
      },
      capacityProviderState,
      capacityWindowState,
      capacityWindowLabel,
      capacityStateText,
      sectionVisibility,
      displayAliases,
      displayAlias,
      chartScale,
      trendSegments,
      relativeDuration,
      snapshotDecision,
      telemetryRefreshUrl,
      snapshotRefreshSlot,
      nextSnapshotRefreshMillis,
      checkForNewSnapshot,
      adoptSnapshot,
    };
  }

  function render() {
    active = windows[activeKey] || active;
    document.querySelectorAll("[data-window]").forEach(button => button.setAttribute("aria-pressed", button.dataset.window === activeKey ? "true" : "false"));
    $("window-summary").textContent = `${active.from} → ${active.to} UTC`;
    $("comparison-note").textContent = active.comparison?.summary ? `Changes vs preceding ${active.inclusive_days} days` : "";
    renderMasthead();
    renderOverview();
    renderActivity();
    renderMix();
    renderAttention();
    renderEconomics();
    renderOutcomes();
    renderReliability();
    const visible = sectionVisibility(active);
    [["investment", visible.results], ["attention", visible.attention], ["outcomes", visible.loop]].forEach(([id, show]) => {
      $(id).hidden = !show;
      const link = document.querySelector(`nav a[href="#${id}"]`);
      if (link) link.hidden = !show;
    });
    $("generated-foot").textContent = `Generated ${when(data.generated_at)}`;
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
  document.querySelectorAll("details.health-disclosure").forEach(detail => detail.addEventListener("toggle", () => {
    const body = detail.querySelector("[data-lazy-body]");
    if (body && detail.open && body.dataset.built !== "true") lazyBody(detail.id);
    updateTestHook();
  }));
  document.addEventListener("click", event => {
    const capacityLink = event.target.closest && event.target.closest('a[href="#capacity-now"]');
    if (capacityLink) {
      event.preventDefault();
      $("usage-sidebar").open = true;
      $("usage-sidebar").querySelector("summary").focus({preventScroll:true});
    }
    const button = event.target.closest && event.target.closest("[data-explain]");
    if (button) showMetric(button.dataset.explain);
  });
  document.addEventListener("keydown", event => {
    const point = event.target.closest && event.target.closest("[data-point-label]");
    if (point && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const points = [...point.closest("svg").querySelectorAll(`[data-series="${point.dataset.series}"]`)];
      const index = points.indexOf(point);
      const next = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1
        : Math.max(0, Math.min(points.length - 1, index + (event.key === "ArrowRight" ? 1 : -1)));
      point.tabIndex = -1;
      points[next].tabIndex = 0;
      points[next].focus({preventScroll:true});
    }
    if (event.key === "Escape" && $("usage-sidebar").open && !$("metric-dialog").open) {
      $("usage-sidebar").open = false;
      $("usage-sidebar").querySelector("summary").focus({preventScroll:true});
    }
  });
  const showPoint = event => {
    const point = event.target.closest && event.target.closest("[data-point-label]");
    if (point) point.closest(".chart-card").querySelector(".chart-tooltip").textContent = point.dataset.pointLabel;
  };
  const clearPoint = event => {
    const chart = event.target.closest && event.target.closest(".chart-card");
    if (chart && !chart.contains(event.relatedTarget)) {
      const tooltip = chart.querySelector(".chart-tooltip");
      if (tooltip) tooltip.textContent = "";
    }
  };
  document.addEventListener("pointerover", showPoint);
  document.addEventListener("focusin", showPoint);
  document.addEventListener("pointerout", clearPoint);
  document.addEventListener("focusout", clearPoint);
  $("metric-dialog-close").addEventListener("click", () => $("metric-dialog").close());
  $("metric-dialog").addEventListener("close", () => {
    restoreFocusState(metricReturnFocus);
    metricReturnFocus = null;
  });
  const refreshAfterVisibilityReturn = () => {
    refreshClientTime();
    checkForNewSnapshot();
  };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshAfterVisibilityReturn(); });
  window.addEventListener("focus", refreshAfterVisibilityReturn);
  window.addEventListener("pageshow", refreshAfterVisibilityReturn);
  if (data.payload_kind !== "bounded_page_envelope") {
    document.body.innerHTML = '<main class="shell"><section><h1>Telemetry unavailable</h1><p class="empty">The bounded page envelope is missing or incompatible.</p></section></main>';
    return;
  }
  renderCapacity();
  render();
  scheduleSnapshotRefresh();
  window.setInterval(refreshClientTime, 60000);
})();
