(() => {
  "use strict";

  const data = window.TELEMETRY || {};
  const metrics = data.metrics || {};
  const now = metrics.now || {};
  const cost = metrics.cost || {};
  const time = metrics.time_v2 || {};
  const rawLedger = (metrics.ledger && metrics.ledger.specs) || [];
  const rawRounds = (metrics.ledger && metrics.ledger.rounds) || [];
  const dailyCost = cost.daily || [];
  const dailyQuality = data.history || [];
  const measurement = metrics.measurement || {};
  const reliability = metrics.reliability || {};
  const observatory = metrics.observatory || {};
  const globalDays = observatory.daily || [];
  const globalProjects = observatory.projects || [];
  const vendorNames = {anthropic: "Anthropic / Claude", openai: "OpenAI / GPT"};
  const bySpec = Object.fromEntries(rawLedger.map(item => [item.spec, item]));
  const $ = id => document.getElementById(id);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const sum = values => values.reduce((total, value) => total + (finite(value) ? value : 0), 0);
  const nf = new Intl.NumberFormat("en-US", {maximumFractionDigits: 1, notation: "compact"});
  const full = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});
  const money = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", maximumFractionDigits: 2});
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[char]);

  function fmtBytes(value) {
    if (!finite(value)) return "n/a";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let amount = value;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${full.format(amount)} ${units[index]}`;
  }

  function fmt(value, kind = "number", reason = "source did not provide this value") {
    if (!finite(value)) return `<span class="empty">n/a · ${esc(reason)}</span>`;
    if (kind === "money") return money.format(value);
    if (kind === "percent") return `${full.format(value * 100)}%`;
    if (kind === "tokens") return nf.format(value);
    if (kind === "hours") return `${full.format(value)}h`;
    if (kind === "minutes") return `${full.format(value)}m`;
    return full.format(value);
  }

  function when(value) {
    return value ? new Date(value).toLocaleString([], {year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short"}) : "n/a · timestamp unavailable";
  }

  function median(values) {
    const clean = values.filter(finite).sort((a, b) => a - b);
    if (!clean.length) return null;
    const middle = Math.floor(clean.length / 2);
    return clean.length % 2 ? clean[middle] : (clean[middle - 1] + clean[middle]) / 2;
  }

  function percentile(values, fraction) {
    const clean = values.filter(finite).sort((a, b) => a - b);
    if (!clean.length) return null;
    if (clean.length === 1) return clean[0];
    const position = (clean.length - 1) * fraction;
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    return clean[lower] + (clean[upper] - clean[lower]) * (position - lower);
  }

  const card = (label, value, detail = "", delta = "") => `<div class="card"><span class="label">${esc(label)}</span><span class="value">${value}</span>${detail ? `<span class="detail">${detail}</span>` : ""}${delta ? `<div class="delta">${delta}</div>` : ""}</div>`;

  function bars(rows, labelKey, aKey, oKey, formatter = value => fmt(value, "money")) {
    const maximum = Math.max(1, ...rows.map(row => (row[aKey] || 0) + (row[oKey] || 0)));
    return rows.map(row => {
      const a = row[aKey] || 0;
      const o = row[oKey] || 0;
      return `<div class="bar-row"><span class="bar-label" title="${esc(row[labelKey])}">${esc(row[labelKey])}</span><span class="track" aria-hidden="true"><span class="fill-a" style="width:${a / maximum * 100}%"></span><span class="fill-o" style="width:${o / maximum * 100}%"></span></span><span class="bar-number">${formatter(a + o)}</span></div>`;
    }).join("");
  }

  const ymd = value => typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : null;
  const validDay = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
  function addDays(day, amount) {
    const date = new Date(`${day}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + amount);
    return date.toISOString().slice(0, 10);
  }
  const inclusiveDays = (from, to) => Math.floor((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000) + 1;
  const inRange = (day, range) => Boolean(day && day >= range.from && day <= range.to);

  const availableDates = [
    ...dailyCost.map(item => item.date),
    ...dailyQuality.map(item => item.date),
    ...rawRounds.map(item => ymd(item.ended_at)),
    ...(time.activity_by_day || []).map(item => item.date),
    ...globalDays.map(item => item.date),
  ].filter(validDay).sort();
  const fallbackDay = ymd(data.collection && data.collection.date) || new Date().toISOString().slice(0, 10);
  const availableFrom = availableDates[0] || fallbackDay;
  const availableTo = availableDates[availableDates.length - 1] || fallbackDay;

  function normalizeRange(from, to) {
    let start = validDay(from) ? from : availableFrom;
    let end = validDay(to) ? to : availableTo;
    start = start < availableFrom ? availableFrom : start > availableTo ? availableTo : start;
    end = end < availableFrom ? availableFrom : end > availableTo ? availableTo : end;
    if (start > end) [start, end] = [end, start];
    return {from: start, to: end};
  }

  const params = new URLSearchParams(window.location.search);
  let activeRange = normalizeRange(params.get("from"), params.get("to"));
  let ledgerSort = {key: "spec", direction: 1};
  let projectSort = {key: "cost_usd", direction: -1};
  let currentLedger = [];
  let currentProjects = [];

  function estimateFromParts(parts) {
    let low = 0;
    let midpoint = 0;
    let high = 0;
    let estimatedTokens = 0;
    let unpricedTokens = 0;
    for (const part of parts) {
      for (const model of Object.values((part && part.models) || {})) {
        const estimate = model && model.best_effort_estimate;
        unpricedTokens += Number(model && model.unpriced_tokens) || 0;
        if (estimate && finite(estimate.low_usd) && finite(estimate.high_usd)) {
          low += estimate.low_usd;
          midpoint += finite(estimate.midpoint_usd) ? estimate.midpoint_usd : (estimate.low_usd + estimate.high_usd) / 2;
          high += estimate.high_usd;
          estimatedTokens += Number(estimate.estimated_tokens) || 0;
        }
      }
    }
    return {low, midpoint, high, estimatedTokens, unpricedTokens};
  }

  function groupLedger(rounds) {
    const groups = new Map();
    for (const round of rounds) {
      const spec = round.spec || "unknown";
      if (!groups.has(spec)) groups.set(spec, []);
      groups.get(spec).push(round);
    }
    return [...groups.entries()].map(([spec, values]) => {
      values.sort((a, b) => (a.round || 0) - (b.round || 0));
      const source = bySpec[spec] || {};
      const acceptedRound = values.find(item => item.accepted);
      const starts = values.map(item => Date.parse(item.started_at)).filter(Number.isFinite);
      const ends = values.map(item => Date.parse(item.ended_at)).filter(Number.isFinite);
      const builderTokens = sum(values.map(item => item.builder && item.builder.tokens));
      const judgeTokens = sum(values.map(item => item.judge && item.judge.tokens));
      const builderUsd = sum(values.map(item => item.builder && item.builder.usd));
      const judgeUsd = sum(values.map(item => item.judge && item.judge.usd));
      const judgeByVendor = {};
      for (const item of values) {
        const vendor = item.judge && item.judge.vendor || "unknown";
        judgeByVendor[vendor] = (judgeByVendor[vendor] || 0) + Number(item.judge && item.judge.usd || 0);
      }
      const estimate = estimateFromParts(values.flatMap(item => [item.builder, item.judge]));
      return {
        spec,
        row: values[0].row || source.row || "unknown",
        outcome: acceptedRound ? "accepted" : String(values[values.length - 1].verdict || "not accepted").toLowerCase(),
        accepted: Boolean(acceptedRound),
        rounds_count: values.length,
        wall_hours: starts.length && ends.length ? Math.max(0, (Math.max(...ends) - Math.min(...starts)) / 3600000) : null,
        lead_hours: acceptedRound ? source.lead_hours : null,
        lead_time_status: acceptedRound ? source.lead_time_status : "not_accepted_in_selected_range",
        tokens: builderTokens + judgeTokens,
        usd: builderUsd + judgeUsd,
        unpriced_tokens: sum(values.map(item => item.unpriced_tokens)),
        estimate_low_usd: estimate.estimatedTokens ? estimate.low : null,
        estimate_midpoint_usd: estimate.estimatedTokens ? estimate.midpoint : null,
        estimate_high_usd: estimate.estimatedTokens ? estimate.high : null,
        build: {tokens: builderTokens, usd: builderUsd},
        judge: {tokens: judgeTokens, usd: judgeUsd, usd_by_vendor: judgeByVendor},
        debt_at_accept: acceptedRound ? acceptedRound.debt_at_accept : null,
        findings_total: sum(values.map(item => item.findings)),
        rounds: values,
      };
    }).sort((a, b) => a.spec.localeCompare(b.spec));
  }

  function worthStats(ledger) {
    const accepted = ledger.filter(item => item.accepted);
    const timed = accepted.filter(item => finite(item.wall_hours));
    const total = field => sum(accepted.map(item => item[field]));
    return {
      accepted: accepted.length,
      terminal: ledger.length,
      efficiency: ledger.length ? accepted.length / ledger.length : null,
      mean: {
        usd: accepted.length ? total("usd") / accepted.length : null,
        hours: timed.length ? sum(timed.map(item => item.wall_hours)) / timed.length : null,
        rounds: accepted.length ? total("rounds_count") / accepted.length : null,
        tokens: accepted.length ? total("tokens") / accepted.length : null,
      },
      median: {
        usd: median(accepted.map(item => item.usd)),
        hours: median(timed.map(item => item.wall_hours)),
        rounds: median(accepted.map(item => item.rounds_count)),
        tokens: median(accepted.map(item => item.tokens)),
      },
      estimate: {
        low: accepted.length ? sum(accepted.map(item => item.estimate_low_usd)) / accepted.length : null,
        high: accepted.length ? sum(accepted.map(item => item.estimate_high_usd)) / accepted.length : null,
      },
      unpriced: total("unpriced_tokens"),
    };
  }

  function deltaText(current, previous, kind) {
    if (!finite(current) || !finite(previous)) return "↔ n/a vs prior equal period";
    const delta = current - previous;
    const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "↔";
    const value = kind === "money" ? money.format(Math.abs(delta)) : kind === "percent" ? `${full.format(Math.abs(delta) * 100)} pp` : kind === "tokens" ? nf.format(Math.abs(delta)) : kind === "hours" ? `${full.format(Math.abs(delta))}h` : full.format(Math.abs(delta));
    return `${arrow} ${delta > 0 ? "+" : delta < 0 ? "−" : ""}${value} vs prior ${inclusiveDays(activeRange.from, activeRange.to)}d`;
  }

  function isoWeek(day) {
    const date = new Date(`${day}T00:00:00Z`);
    const weekday = date.getUTCDay() || 7;
    date.setUTCDate(date.getUTCDate() + 4 - weekday);
    const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
    const week = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
  }

  function aggregateGlobal(rows) {
    const vendors = Object.fromEntries(["anthropic", "openai"].map(vendor => [vendor, {sessions: 0, tokens: 0, cost: 0, unpriced: 0}]));
    const hosts = Object.fromEntries(["wsl", "windows"].map(host => [host, {sessions: 0, tokens: 0, cost: 0, unpriced: 0}]));
    const projects = new Map();
    const dates = new Map();
    for (const row of rows) {
      const vendor = vendors[row.vendor] || (vendors[row.vendor] = {sessions: 0, tokens: 0, cost: 0, unpriced: 0});
      const host = hosts[row.host_os] || (hosts[row.host_os] = {sessions: 0, tokens: 0, cost: 0, unpriced: 0});
      for (const target of [vendor, host]) {
        target.sessions += Number(row.sessions) || 0;
        target.tokens += Number(row.tokens) || 0;
        target.cost += Number(row.cost_usd) || 0;
        target.unpriced += Number(row.unpriced_tokens) || 0;
      }
      const project = projects.get(row.project_id) || {project_id: row.project_id, sessions: 0, tokens: 0, cost_usd: 0, unpriced_tokens: 0, anthropic_tokens: 0, openai_tokens: 0, anthropic_cost: 0, openai_cost: 0};
      project.sessions += Number(row.sessions) || 0;
      project.tokens += Number(row.tokens) || 0;
      project.cost_usd += Number(row.cost_usd) || 0;
      project.unpriced_tokens += Number(row.unpriced_tokens) || 0;
      if (row.vendor === "anthropic") { project.anthropic_tokens += Number(row.tokens) || 0; project.anthropic_cost += Number(row.cost_usd) || 0; }
      if (row.vendor === "openai") { project.openai_tokens += Number(row.tokens) || 0; project.openai_cost += Number(row.cost_usd) || 0; }
      projects.set(row.project_id, project);
      const day = dates.get(row.date) || {date: row.date, anthropic: 0, openai: 0, tokens: 0, sessions: 0};
      day[row.vendor] = (day[row.vendor] || 0) + (Number(row.cost_usd) || 0);
      day.tokens += Number(row.tokens) || 0;
      day.sessions += Number(row.sessions) || 0;
      dates.set(row.date, day);
    }
    return {vendors, hosts, projects: [...projects.values()], dates: [...dates.values()].sort((a, b) => a.date.localeCompare(b.date))};
  }

  function setSelfCheck(id, ok, goodText, badText) {
    const target = $(id);
    if (!target) return;
    target.className = `status ${ok ? "good" : "warn"}`;
    target.textContent = ok ? goodText : badText;
  }

  function renderNow() {
    const totals = observatory.totals || {};
    const roots = observatory.source_roots || [];
    const reconciliation = observatory.reconciliation || {};
    $("mast-meta").innerHTML = `Collected ${esc(when(data.generated_at))}<br>${roots.filter(item => item.status !== "absent").length} of ${roots.length || 4} global roots reachable · ${esc(observatory.coverage && observatory.coverage.from ? `${ymd(observatory.coverage.from)} → ${ymd(observatory.coverage.to)}` : "coverage pending")}`;
    const contractOk = observatory.store && observatory.store.integrity === "ok" && reconciliation.status === "ok" && roots.length >= 4;
    setSelfCheck("now-check", contractOk, "Store + joins OK", "Coverage degraded");
    const banners = [];
    const degradedRoots = roots.filter(item => item.status !== "ok");
    if (degradedRoots.length) banners.push(`<div class="banner warn"><strong>Named source state.</strong> ${degradedRoots.map(item => `${esc(item.root_id)}: ${esc(item.status)} (${esc(item.detail || "detail unavailable")})`).join(" · ")}. Cached last-good data remains visible.</div>`);
    if (Number(totals.unpriced_tokens) > 0) banners.push(`<div class="banner warn"><strong>Exact-cost boundary.</strong> ${fmt(totals.unpriced_tokens, "tokens")} tokens remain real usage but are excluded from exact dollars because no exact observed-model price applies.</div>`);
    if (now.stalled) banners.push(`<div class="banner bad"><strong>Driver stalled.</strong> Active row ${esc(now.current_row || "unknown")} has been silent for ${fmt(now.minutes_since_driver)} minutes; threshold ${fmt(now.stall_threshold_minutes)}.</div>`);
    if (now.publish_stale) banners.push(`<div class="banner warn"><strong>Publish stale.</strong> ${now.last_publish_at ? `Last success ${esc(when(now.last_publish_at))}.` : "No successful scheduled publish has been recorded."} State: ${esc(now.publish_status || "unknown")} / ${esc(now.publish_reason || "reason unavailable")}.</div>`);
    $("now-banners").innerHTML = banners.join("");
    $("now-cards").innerHTML = [
      card("Deduplicated sessions", fmt(totals.sessions), `${fmt(observatory.observations && observatory.observations.deduplicated)} duplicate observations removed`),
      card("Machine-wide tokens", fmt(totals.tokens, "tokens"), "Vendor-correct lifetime total"),
      card("Exact API-equivalent", fmt(totals.cost_usd, "money"), "Observed model strings only"),
      card("Unpriced tokens", fmt(totals.unpriced_tokens, "tokens"), "Counted; never silently priced"),
      card("Project identities", fmt(globalProjects.length), `${fmt(observatory.unregistered_candidates && observatory.unregistered_candidates.count)} unregistered candidate clusters`),
    ].join("");
    $("host-cards").innerHTML = ["wsl", "windows"].map(host => {
      const item = observatory.by_host_os && observatory.by_host_os[host] || {};
      return card(host === "wsl" ? "WSL-hosted" : "Windows-hosted", fmt(item.sessions), `${fmt(item.tokens, "tokens")} tokens · ${fmt(item.cost_usd, "money")} exact`);
    }).join("");
    const today = now.today || {};
    $("driver-cards").innerHTML = [
      card("Current row", now.current_row ? esc(now.current_row) : "Idle", `Flagship state · ${esc(now.current_state || "unknown")}`),
      card("Today", fmt(today.rounds), `${fmt(today.events)} events · ${fmt(today.merges)} merges`),
      card("Driver freshness", fmt(now.minutes_since_driver), "minutes since latest flagship event"),
      card("Last publish", esc(when(now.last_publish_at)), `${esc(now.publish_status || "unknown")} · ${esc(now.publish_reason || "reason unavailable")}`),
    ].join("");
  }

  function dataAgeMinutes() {
    const generated = Date.parse(data.generated_at);
    return Number.isFinite(generated) ? Math.max(0, (Date.now() - generated) / 60000) : null;
  }

  function updateClientDataAge() {
    const target = $("client-data-age");
    if (!target) return;
    const age = dataAgeMinutes();
    target.textContent = finite(age) ? `${full.format(age)}m` : "n/a";
    target.className = finite(age) && age > 45 ? "warn" : "";
    target.title = "Computed in this browser from generated_at; no server clock required.";
  }

  function renderReliability() {
    const cadence = reliability.cadence || {};
    const disk = reliability.disk || {};
    const checks = reliability.checks || [];
    const warnings = checks.filter(item => item.status === "warn").length;
    const failures = checks.filter(item => item.status === "fail").length;
    const runway = finite(disk.runway_years) ? `${full.format(disk.runway_years)} years` : "pending";
    const diskMethod = String(disk.headline || "measurement pending").replaceAll("_", " ");
    $("reliability-cards").innerHTML = [
      card("Data age · live browser clock", `<span id="client-data-age">calculating…</span>`, "Updates each minute and works from file://"),
      card("Collection gaps", fmt(cadence.missed_intervals), `${fmt(cadence.observed_starts)} observed starts · longest ${fmt(cadence.longest_gap_minutes, "minutes")}`),
      card("Doctor", esc(reliability.status || "unknown"), `${failures} failed · ${warnings} warnings · ${checks.length} checks`),
      card("Disk runway", esc(runway), `${fmtBytes(disk.free_bytes)} free · ${esc(diskMethod)}`),
    ].join("");
    updateClientDataAge();
  }

  function renderWorth(filteredLedger, previousLedger) {
    const stats = worthStats(filteredLedger);
    const prior = worthStats(previousLedger);
    const subscription = metrics.worth && metrics.worth.subscription_amortization || {};
    const rangeDays = inclusiveDays(activeRange.from, activeRange.to);
    const periodSubscription = finite(subscription.daily_total_usd) ? subscription.daily_total_usd * rangeDays : null;
    const subscriptionPerAccepted = stats.accepted && finite(periodSubscription) ? periodSubscription / stats.accepted : null;
    const estimateDetail = stats.unpriced && finite(stats.estimate.low) && finite(stats.estimate.high) ? ` · estimated add ${money.format(stats.estimate.low)}–${money.format(stats.estimate.high)}` : "";
    $("worth-cards").innerHTML = [
      card("Mean exact API cost / accepted", fmt(stats.mean.usd, "money"), `median ${fmt(stats.median.usd, "money")} · ${nf.format(stats.unpriced)} unpriced tokens${estimateDetail}`, deltaText(stats.mean.usd, prior.mean.usd, "money")),
      card("Subscription / accepted", fmt(subscriptionPerAccepted, "money", subscription.reason || "subscription config unavailable"), finite(periodSubscription) ? `${money.format(periodSubscription)} prorated over ${rangeDays} calendar days · ${money.format(subscription.monthly_total_usd)} / month` : esc(subscription.reason || "local subscription config unavailable")),
      card("Mean wall time / accepted", fmt(stats.mean.hours, "hours"), `median ${fmt(stats.median.hours, "hours")}`, deltaText(stats.mean.hours, prior.mean.hours, "hours")),
      card("Mean rounds / accepted", fmt(stats.mean.rounds), `median ${fmt(stats.median.rounds)}`, deltaText(stats.mean.rounds, prior.mean.rounds, "number")),
      card("Acceptance efficiency", fmt(stats.efficiency, "percent"), `${fmt(stats.accepted)} accepted specs · ${fmt(stats.terminal)} terminal specs`, deltaText(stats.efficiency, prior.efficiency, "percent")),
    ].join("");
  }

  function renderProjectTable() {
    const lifetime = Object.fromEntries(globalProjects.map(item => [item.project_id, item]));
    const ordered = [...currentProjects].sort((a, b) => {
      const av = a[projectSort.key] ?? (typeof a[projectSort.key] === "string" ? "" : -Infinity);
      const bv = b[projectSort.key] ?? (typeof b[projectSort.key] === "string" ? "" : -Infinity);
      return (typeof av === "string" ? av.localeCompare(bv) : av - bv) * projectSort.direction;
    });
    document.querySelectorAll("#project-table th[data-key]").forEach(th => th.setAttribute("aria-sort", th.dataset.key === projectSort.key ? (projectSort.direction === 1 ? "ascending" : "descending") : "none"));
    $("project-body").innerHTML = ordered.map(item => {
      const meta = lifetime[item.project_id] || {};
      const label = meta.public_label || item.project_id;
      const privacy = meta.public_label ? "approved label" : item.project_id.startsWith("proj-") ? "anonymous code" : "bulk bucket";
      return `<tr><td><strong>${esc(label)}</strong><span class="detail">${esc(meta.category || "project")} · ${privacy}</span></td><td class="num">${fmt(item.sessions)}</td><td class="num">${fmt(item.anthropic_tokens, "tokens")}<span class="detail">${fmt(item.anthropic_cost, "money")}</span></td><td class="num">${fmt(item.openai_tokens, "tokens")}<span class="detail">${fmt(item.openai_cost, "money")}</span></td><td class="num">${fmt(item.tokens, "tokens")}</td><td class="num">${fmt(item.cost_usd, "money")}</td><td class="num">${fmt(item.unpriced_tokens, "tokens")}</td><td>${esc(when(meta.last_seen_at))}</td></tr>`;
    }).join("") || `<tr><td colspan="8" class="empty">n/a · no project-attributed usage in the selected UTC range</td></tr>`;
  }

  function renderProjects(globalAggregate) {
    currentProjects = globalAggregate.projects;
    const active = currentProjects.length;
    const registered = globalProjects.filter(item => !["ad-hoc", "remote"].includes(item.project_code)).length;
    const adHoc = currentProjects.find(item => item.project_id === "ad-hoc") || {};
    const remote = currentProjects.find(item => item.project_id === "remote") || {};
    $("project-cards").innerHTML = [
      card("Active identities", fmt(active), `${registered} registered identities exist across lifetime coverage`),
      card("Selected exact cost", fmt(sum(currentProjects.map(item => item.cost_usd)), "money"), `${fmt(sum(currentProjects.map(item => item.tokens)), "tokens")} tokens`),
      card("Ad-hoc bulk", fmt(adHoc.sessions), `${fmt(adHoc.tokens, "tokens")} tokens · selected session-days`),
      card("Remote bulk", fmt(remote.sessions), `${fmt(remote.tokens, "tokens")} tokens · selected session-days`),
    ].join("");
    const projectTokens = sum(currentProjects.map(item => item.tokens));
    const vendorTokens = sum(Object.values(globalAggregate.vendors).map(item => item.tokens));
    setSelfCheck("projects-check", projectTokens === vendorTokens, "Project totals reconcile", "Project totals differ");
    renderProjectTable();
  }

  function renderCost(globalAggregate) {
    $("price-note").textContent = `Exact-string API-equivalent rates verified ${cost.prices && cost.prices.verified_at || "n/a"}. OpenAI long-context pricing is applied only when per-turn evidence supports it; subscription invoices are a separate local view.`;
    $("vendor-cards").innerHTML = ["anthropic", "openai"].map(vendor => {
      const item = globalAggregate.vendors[vendor] || {};
      const lifetime = observatory.by_vendor && observatory.by_vendor[vendor] || {};
      const quota = cost.usage_left && cost.usage_left[vendor] || {};
      const quotaText = finite(quota.remaining_percent) ? `${full.format(quota.remaining_percent)}% remaining` : `n/a · ${quota.remaining_status || "no current quota observation"}`;
      return `<div class="card"><div style="display:flex;justify-content:space-between;gap:12px"><div><span class="label">${vendorNames[vendor]} · selected range</span><span class="value">${fmt(item.cost, "money")}</span></div><span class="status ${finite(quota.remaining_percent) ? "good" : "warn"}">${quotaText}</span></div><span class="detail">${fmt(item.tokens, "tokens")} tokens · ${fmt(item.sessions)} session-days · ${fmt(item.unpriced, "tokens")} unpriced</span><span class="detail">Lifetime: ${fmt(lifetime.sessions)} sessions · ${fmt(lifetime.tokens, "tokens")} tokens · ${fmt(lifetime.cost_usd, "money")} exact</span><span class="detail">Quota source ${esc(quota.source || "unavailable")} · ${esc(when(quota.observed_at))}</span></div>`;
    }).join("");
    const dailyRows = globalAggregate.dates.map(day => ({...day, date: day.date.slice(5)}));
    $("daily-cost").innerHTML = dailyRows.length ? bars(dailyRows, "date", "anthropic", "openai") + `<span class="detail"><span class="good">■ Anthropic</span> · <span class="neutral">■ OpenAI</span></span>` : `<span class="empty">n/a · no cost snapshots in selected range</span>`;
    const hostRows = ["wsl", "windows"].map(host => ({host: host === "wsl" ? "WSL-hosted" : "Windows-hosted", tokens: globalAggregate.hosts[host].tokens, none: 0}));
    $("host-usage").innerHTML = bars(hostRows, "host", "tokens", "none", value => fmt(value, "tokens"));
    $("root-health").innerHTML = (observatory.source_roots || []).map(root => `<div class="source"><strong class="${root.status === "ok" ? "good" : "warn"}">${esc(root.root_id)} · ${esc(root.status)}</strong><span>${fmt(root.files)} files · ${fmt(root.scan_seconds)}s · ${esc(root.strategy || "strategy unavailable")}</span><br><span>${fmt(root.files_changed)} changed · ${fmt(root.files_reused)} reused</span></div>`).join("");
    const selectedTokens = sum(Object.values(globalAggregate.vendors).map(item => item.tokens));
    const projectTokens = sum(globalAggregate.projects.map(item => item.tokens));
    setSelfCheck("cost-check", selectedTokens === projectTokens, "Range totals reconcile", "Range totals differ");
  }

  function renderTime(filteredRounds, globalAggregate) {
    const durations = filteredRounds.map(item => item.duration_minutes).filter(finite);
    const rows = (time.rows || []).filter(item => inRange(ymd(item.terminal_at), activeRange));
    const phases = {};
    for (const row of rows) for (const [phase, hours] of Object.entries(row.phases_hours || {})) phases[phase] = (phases[phase] || 0) + (Number(hours) || 0);
    const peak = [...globalAggregate.dates].sort((a, b) => b.tokens - a.tokens)[0];
    const selectedTokens = sum(globalAggregate.dates.map(item => item.tokens));
    $("time-cards").innerHTML = [
      card("Active UTC days", fmt(globalAggregate.dates.length), `${activeRange.from} → ${activeRange.to}`),
      card("Tokens / active day", fmt(globalAggregate.dates.length ? selectedTokens / globalAggregate.dates.length : null, "tokens", "no provider events in range"), "Machine-wide mean"),
      card("Peak usage day", peak ? esc(peak.date) : `<span class="empty">n/a · no provider events</span>`, peak ? `${fmt(peak.tokens, "tokens")} tokens · ${fmt(peak.sessions)} session-days` : ""),
      card("Flagship round duration", fmt(median(durations), "minutes", "no complete rounds"), `p95 ${fmt(percentile(durations, 0.95), "minutes")} · ${durations.length} rounds`),
    ].join("");

    const weeks = {};
    for (const round of filteredRounds) {
      const day = ymd(round.ended_at);
      if (!day || !finite(round.duration_minutes)) continue;
      const week = isoWeek(day);
      if (!weeks[week]) weeks[week] = [];
      weeks[week].push(round.duration_minutes);
    }
    const trend = Object.entries(weeks).sort().map(([week, values]) => ({week, median: median(values) || 0, p95: Math.max(0, (percentile(values, 0.95) || 0) - (median(values) || 0))}));
    $("round-trend").innerHTML = trend.length ? bars(trend, "week", "median", "p95", value => `${full.format(value)}m`) + `<span class="detail"><span class="good">■ median</span> · <span class="neutral">■ p95 tail</span></span>` : `<span class="empty">n/a · no completed round windows</span>`;
    const phaseRows = Object.entries(phases).map(([phase, hours]) => ({phase: phase.replaceAll("_", " "), hours, none: 0}));
    $("phase-time").innerHTML = phaseRows.length ? bars(phaseRows, "phase", "hours", "none", value => `${full.format(value)}h`) : `<span class="empty">n/a · no terminal rows in selected range</span>`;

    const heatCounts = Array.from({length: 7}, () => Array(24).fill(0));
    const hourRows = (observatory.activity_hours || []).filter(item => inRange(item.date, activeRange));
    for (const item of hourRows) {
      const weekday = (Number(item.weekday_utc) + 6) % 7;
      const hour = Number(item.hour_utc);
      if (weekday >= 0 && weekday < 7 && hour >= 0 && hour < 24) heatCounts[weekday][hour] += Number(item.events) || 0;
    }
    const maximum = Math.max(1, ...heatCounts.flat());
    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    let heatHtml = `<span></span>${Array.from({length: 24}, (_, hour) => `<span class="hour">${hour}</span>`).join("")}`;
    weekdays.forEach((day, weekday) => {
      heatHtml += `<span class="day">${day}</span>`;
      for (let hour = 0; hour < 24; hour++) {
        const events = heatCounts[weekday][hour];
        heatHtml += `<span class="cell" style="--intensity:${events / maximum}" title="${day} ${hour}:00 UTC · ${events} selected events">${events}</span>`;
      }
    });
    $("heatmap").innerHTML = heatHtml;
    setSelfCheck("time-check", globalAggregate.dates.length > 0 && hourRows.length > 0, "UTC activity covered", "Time coverage partial");
  }

  function renderQuality(filteredRounds, filteredLedger, qualityDays) {
    const acceptedRounds = filteredRounds.filter(item => item.accepted).length;
    const distinct = filteredRounds.filter(item => item.builder && item.judge && item.builder.vendor !== item.judge.vendor).length;
    const proofs = sum(qualityDays.map(item => item.proofs));
    const proofFailures = sum(qualityDays.map(item => item.proof_failures));
    const latestTestDay = [...qualityDays].reverse().find(item => finite(item.latest_tests));
    const acceptedSpecs = filteredLedger.filter(item => item.accepted);
    const legacy = [
      ["Accepted rows", acceptedSpecs.length],
      ["Judge rounds", filteredRounds.length],
      ["Judge acceptance", fmt(filteredRounds.length ? acceptedRounds / filteredRounds.length : null, "percent")],
      ["Distinct-vendor", fmt(filteredRounds.length ? distinct / filteredRounds.length : null, "percent")],
      ["Median judge round", fmt(median(filteredRounds.map(item => item.duration_minutes)), "minutes")],
      ["Median rounds / accepted", median(acceptedSpecs.map(item => item.rounds_count))],
      ["Proof error rate", fmt(proofs ? proofFailures / proofs : null, "percent")],
      ["Latest tests in range", latestTestDay ? latestTestDay.latest_tests : null],
      ["Latest test duration", latestTestDay ? `${fmt(latestTestDay.latest_test_seconds)}s` : `<span class="empty">n/a · no test run in range</span>`],
    ];
    $("legacy-health").innerHTML = legacy.map(([label, value]) => `<div class="mini"><span class="label">${esc(label)}</span><b>${finite(value) ? fmt(value) : value ?? `<span class="empty">n/a</span>`}</b></div>`).join("");

    const qualityRows = [...filteredLedger].sort((a, b) => b.usd - a.usd).slice(0, 10);
    const maximum = Math.max(1, ...qualityRows.map(item => item.usd));
    $("quality-spend").innerHTML = qualityRows.map(item => `<div class="bar-row"><span class="bar-label" title="${esc(item.spec)}">${esc(item.spec)}</span><span class="track"><span class="fill-a" style="width:${item.usd / maximum * 100}%"></span></span><span class="bar-number">${money.format(item.usd)} · ${item.findings_total} findings</span></div>`).join("") || `<span class="empty">n/a · no per-spec cost in selected range</span>`;

    const errors = metrics.errors || {};
    const failures = Object.entries(errors.failures_by_group || {}).map(([group, count]) => ({group, count, none: 0})).sort((a, b) => b.count - a.count);
    $("failure-groups").innerHTML = failures.length ? bars(failures, "group", "count", "none", value => `${fmt(value)} all-time`) + `<span class="detail">Failure groups are not day-attributed in the source and remain explicitly all-time.</span>` : `<span class="empty">n/a · no proof failure groups observed</span>`;
    const incidents = sum(Object.values(errors.incidents || {}));
    $("quality-cards").innerHTML = [
      card("Selected proof failures", fmt(proofFailures), `${fmt(proofs)} selected proofs`),
      card("All-time incidents", fmt(incidents), "source signals are not day-attributed"),
      card("All-time escalations", fmt(metrics.judges && metrics.judges.escalation_events), `${fmt(metrics.judges && metrics.judges.escalation_clear_events)} cleared · not day-attributed`),
    ].join("");
    const denominatorsOk = !filteredRounds.length || acceptedRounds <= filteredRounds.length;
    setSelfCheck("quality-check", denominatorsOk, "Denominators explicit", "Denominator mismatch");
  }

  const sortValue = (item, key) => item[key] === null || item[key] === undefined ? (typeof item[key] === "string" ? "" : -Infinity) : item[key];
  function modelText(part) {
    const observed = part && part.model_observed || "n/a";
    const declared = part && part.model_declared;
    return declared && declared !== observed ? `${esc(observed)} <em>(declared ${esc(declared)})</em>` : esc(observed);
  }

  function renderLedger() {
    const ordered = [...currentLedger].sort((a, b) => {
      const av = sortValue(a, ledgerSort.key);
      const bv = sortValue(b, ledgerSort.key);
      return (typeof av === "string" ? av.localeCompare(bv) : av - bv) * ledgerSort.direction;
    });
    document.querySelectorAll("#ledger-table th[data-key]").forEach(th => th.setAttribute("aria-sort", th.dataset.key === ledgerSort.key ? (ledgerSort.direction === 1 ? "ascending" : "descending") : "none"));
    $("ledger-body").innerHTML = ordered.map(item => {
      const roundRows = item.rounds.map(round => `<div class="round-row"><strong>R${fmt(round.round)}</strong><span class="model-pair">${esc(round.judge && round.judge.vendor || "vendor n/a")} · ${modelText(round.judge)}</span><span>${fmt(round.duration_minutes, "minutes", "unmatched window")}</span><span>${fmt(round.total_tokens, "tokens")}</span><span>${fmt(round.total_usd, "money")}</span><span>${esc(round.verdict || "n/a")}</span><span>${fmt(round.findings)} findings · ${esc(round.judge && round.judge.attribution || "unattributed")}</span></div>`).join("");
      return `<tr><td><strong>${esc(item.spec)}</strong><span class="detail">row ${esc(item.row)}</span></td><td><span class="status ${item.accepted ? "good" : "warn"}">${esc(item.outcome)}</span></td><td class="num">${fmt(item.rounds_count)}</td><td class="num">${fmt(item.wall_hours, "hours", "round timestamps missing")}</td><td class="num">${fmt(item.lead_hours, "hours", item.lead_time_status)}</td><td class="num">${fmt(item.tokens, "tokens")}</td><td class="num">${fmt(item.usd, "money")}</td><td class="num">${fmt(item.findings_total)}</td><td class="num">${fmt(item.debt_at_accept, "number", "not accepted in range")}</td></tr><tr><td colspan="9"><details><summary>Inspect ${fmt(item.rounds_count)} selected rounds · build ${fmt(item.build.usd, "money")} / judge ${fmt(item.judge.usd, "money")}</summary><div class="round-grid">${roundRows || `<span class="empty">n/a · round details unavailable</span>`}</div></details></td></tr>`;
    }).join("") || `<tr><td colspan="9" class="empty">n/a · no complete rounds in selected range</td></tr>`;
    $("ledger-count").textContent = `${currentLedger.length} specs · ${sum(currentLedger.map(item => item.rounds_count))} complete rounds · selected UTC range`;
    setSelfCheck("ledger-check", currentLedger.every(item => item.rounds_count === item.rounds.length), "Round joins reconcile", "Round joins differ");
  }

  function attributionCounts(rounds, vendor, part) {
    const counts = {exact: 0, correlated: 0, unattributed: 0};
    for (const round of rounds) {
      const value = round[part];
      if (!value || value.vendor !== vendor) continue;
      const tier = Object.hasOwn(counts, value.attribution) ? value.attribution : "unattributed";
      counts[tier] += 1;
    }
    return counts;
  }

  function renderCoverage(filteredRounds) {
    const usageLeft = cost.usage_left || {};
    const lifetimeParity = cost.parity || {};
    const candidateCodes = observatory.unregistered_candidates && observatory.unregistered_candidates.codes || [];
    const candidateGroups = [];
    for (let index = 0; index < candidateCodes.length; index += 10) candidateGroups.push(candidateCodes.slice(index, index + 10));
    $("candidate-list").innerHTML = candidateGroups.length ? candidateGroups.map((group, index) => `<div class="source"><strong>${candidateCodes.length} anonymous cluster${candidateCodes.length === 1 ? "" : "s"} · group ${index + 1}</strong><span>${group.map(esc).join(" · ")}</span></div>`).join("") : `<div class="source"><strong class="good">No unregistered clusters</strong><span>Every observed working-directory cluster is registered or an explicit remote bucket.</span></div>`;
    $("parity-grid").innerHTML = ["anthropic", "openai"].map(vendor => {
      const build = attributionCounts(filteredRounds, vendor, "builder");
      const judge = attributionCounts(filteredRounds, vendor, "judge");
      const parts = filteredRounds.flatMap(round => [round.builder, round.judge]).filter(part => part && part.vendor === vendor);
      const quota = usageLeft[vendor] || {};
      return `<div class="card"><span class="label">${vendorNames[vendor]} · selected range</span><span class="value">${fmt(parts.length)} surfaces</span><div class="table-wrap" style="margin-top:10px"><table style="min-width:480px"><thead><tr><th>Surface</th><th class="num">Exact</th><th class="num">Correlated</th><th class="num">Unattributed</th></tr></thead><tbody><tr><td>Build rounds</td><td class="num">${fmt(build.exact)}</td><td class="num">${fmt(build.correlated)}</td><td class="num">${fmt(build.unattributed)}</td></tr><tr><td>Judge rounds</td><td class="num">${fmt(judge.exact)}</td><td class="num">${fmt(judge.correlated)}</td><td class="num">${fmt(judge.unattributed)}</td></tr></tbody></table></div><span class="detail">Selected captured ${fmt(sum(parts.map(part => part.tokens)), "tokens")} · exact ${fmt(sum(parts.map(part => part.usd)), "money")} · unpriced ${fmt(sum(parts.map(part => part.unpriced_tokens)), "tokens")}</span><span class="detail">Lifetime sessions ${fmt(lifetimeParity[vendor] && lifetimeParity[vendor].sessions_found)} · current usage-left ${finite(quota.remaining_percent) ? `${full.format(quota.remaining_percent)}% · ${esc(quota.source)}` : `n/a · ${esc(quota.remaining_status || "no quota snapshot")}`}</span></div>`;
    }).join("");

    const sourceRows = Object.entries(data.sources || {}).map(([name, item]) => {
      const coverage = item.coverage || {};
      const skips = (item.skips || []).map(skip => `${skip.reason}:${skip.count}`).join(", ") || "none";
      return `<div class="source"><strong>${esc(name)} · ${esc(item.status || "unknown")}</strong><span>${esc(coverage.from || "start n/a")} → ${esc(coverage.to || "end n/a")}</span><br><span>Current skips: ${esc(skips)}</span></div>`;
    });
    const unknown = lifetimeParity.unknown || {};
    if ((unknown.build_rounds || 0) + (unknown.judge_rounds || 0)) {
      sourceRows.push(`<div class="source"><strong class="warn">Unknown vendor evidence</strong><span>${fmt(unknown.build_rounds)} build · ${fmt(unknown.judge_rounds)} judge rounds counted, never priced</span></div>`);
    }
    $("source-list").innerHTML = sourceRows.join("") || `<span class="empty">n/a · source metadata unavailable</span>`;
  }

  function renderMeasurement() {
    const days = (measurement.daily || []).filter(item => inRange(item.date, activeRange));
    const observations = sum(days.map(item => item.observations));
    let sourceOk = 0;
    let sourceTotal = 0;
    const quotaAvailable = {anthropic: 0, openai: 0};
    const quotaTotal = {anthropic: 0, openai: 0};
    const gaps = new Set();
    for (const day of days) {
      for (const source of Object.values(day.sources || {})) {
        for (const [status, count] of Object.entries(source.status_counts || {})) {
          sourceTotal += Number(count) || 0;
          if (status === "ok") sourceOk += Number(count) || 0;
        }
      }
      for (const vendor of ["anthropic", "openai"]) {
        for (const [status, count] of Object.entries(day.vendors && day.vendors[vendor] && day.vendors[vendor].quota_status_counts || {})) {
          quotaTotal[vendor] += Number(count) || 0;
          if (status === "available") quotaAvailable[vendor] += Number(count) || 0;
        }
      }
      for (const gap of day.latest_gaps || []) gaps.add(gap);
    }
    $("measurement-cards").innerHTML = [
      card("Collector observations", fmt(observations), days.length ? `${days.length} measured calendar day${days.length === 1 ? "" : "s"}` : "capability had not started in selected range"),
      card("Healthy source probes", sourceTotal ? fmt(sourceOk / sourceTotal, "percent") : fmt(null, "percent", "no observations in range"), sourceTotal ? `${sourceOk} ok of ${sourceTotal} observed source states` : "no reconstructed history"),
      card("Claude quota observed", quotaTotal.anthropic ? fmt(quotaAvailable.anthropic / quotaTotal.anthropic, "percent") : fmt(null, "percent", "no observations in range"), `${quotaAvailable.anthropic} available of ${quotaTotal.anthropic} observations`),
      card("OpenAI quota observed", quotaTotal.openai ? fmt(quotaAvailable.openai / quotaTotal.openai, "percent") : fmt(null, "percent", "no observations in range"), `${quotaAvailable.openai} available of ${quotaTotal.openai} observations`),
    ].join("");
    $("measurement-gaps").innerHTML = gaps.size ? [...gaps].sort().map(gap => `<div class="source"><strong class="warn">Tracked gap</strong><span>${esc(gap.replaceAll("_", " "))}</span></div>`).join("") : `<div class="source"><strong class="${observations ? "good" : "neutral"}">${observations ? "No tracked gaps" : "No observations"}</strong><span>${observations ? "All observed health dimensions were available." : `Measurement history starts ${esc(when(measurement.started_at))}.`}</span></div>`;
  }

  function render() {
    const filteredRounds = rawRounds.filter(item => inRange(ymd(item.ended_at), activeRange));
    const rangeDays = inclusiveDays(activeRange.from, activeRange.to);
    const priorRange = {from: addDays(activeRange.from, -rangeDays), to: addDays(activeRange.from, -1)};
    const previousRounds = rawRounds.filter(item => inRange(ymd(item.ended_at), priorRange));
    const filteredLedger = groupLedger(filteredRounds);
    const previousLedger = groupLedger(previousRounds);
    const filteredQualityDays = dailyQuality.filter(item => inRange(item.date, activeRange));
    const filteredGlobalDays = globalDays.filter(item => inRange(item.date, activeRange));
    const globalAggregate = aggregateGlobal(filteredGlobalDays);
    currentLedger = filteredLedger;

    $("range-from").value = activeRange.from;
    $("range-to").value = activeRange.to;
    $("range-summary").textContent = `${activeRange.from} → ${activeRange.to} · ${rangeDays} inclusive UTC days · ${globalAggregate.projects.length} active identities · ${filteredRounds.length} flagship rounds. Lifetime cards, quota, and source probes remain point-in-time.`;
    document.querySelectorAll("[data-range]").forEach(button => {
      const preset = button.dataset.range;
      const matches = preset === "all" ? activeRange.from === availableFrom && activeRange.to === availableTo : activeRange.to === availableTo && inclusiveDays(activeRange.from, activeRange.to) === Number(preset);
      button.setAttribute("aria-pressed", matches ? "true" : "false");
    });

    renderNow();
    renderReliability();
    renderProjects(globalAggregate);
    renderWorth(filteredLedger, previousLedger);
    renderCost(globalAggregate);
    renderTime(filteredRounds, globalAggregate);
    renderQuality(filteredRounds, filteredLedger, filteredQualityDays);
    renderLedger();
    renderCoverage(filteredRounds);
    renderMeasurement();
    $("generated-foot").textContent = `Generated ${when(data.generated_at)} · store ${observatory.store && observatory.store.integrity || "unknown"} · reconciliation ${observatory.reconciliation && observatory.reconciliation.status || "pending"} · doctor ${reliability.status || "unknown"} · measurement started ${when(measurement.started_at)}`;
  }

  function setRange(next) {
    activeRange = normalizeRange(next.from, next.to);
    const url = new URL(window.location.href);
    url.searchParams.set("from", activeRange.from);
    url.searchParams.set("to", activeRange.to);
    window.history.replaceState({}, "", url);
    render();
  }

  $("range-from").min = availableFrom;
  $("range-from").max = availableTo;
  $("range-to").min = availableFrom;
  $("range-to").max = availableTo;
  $("range-form").addEventListener("submit", event => {
    event.preventDefault();
    const from = $("range-from").value;
    const to = $("range-to").value;
    if (from > to) {
      $("range-to").setCustomValidity("Through date must be on or after From date.");
      $("range-to").reportValidity();
      return;
    }
    $("range-to").setCustomValidity("");
    setRange({from, to});
  });
  document.querySelectorAll("[data-range]").forEach(button => button.addEventListener("click", () => {
    const preset = button.dataset.range;
    setRange(preset === "all" ? {from: availableFrom, to: availableTo} : {from: addDays(availableTo, -(Number(preset) - 1)), to: availableTo});
  }));
  document.querySelectorAll("#ledger-table th button").forEach(button => button.addEventListener("click", () => {
    const key = button.parentElement.dataset.key;
    ledgerSort = ledgerSort.key === key ? {key, direction: -ledgerSort.direction} : {key, direction: 1};
    renderLedger();
  }));
  document.querySelectorAll("#project-table th button").forEach(button => button.addEventListener("click", () => {
    const key = button.parentElement.dataset.key;
    projectSort = projectSort.key === key ? {key, direction: -projectSort.direction} : {key, direction: typeof currentProjects[0]?.[key] === "string" ? 1 : -1};
    renderProjectTable();
  }));

  render();
  window.setInterval(updateClientDataAge, 60000);
})();
