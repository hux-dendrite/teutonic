(function () {
  "use strict";
  var ENDPOINT = "/dashboard.json";
  var DATASET_MANIFEST_URL = "https://pub-fedac496355c4edc9aed57189e6e190f.r2.dev/datasets/manifest.json";
  var MODEL_STORAGE_BASE = "https://pub-0821d4e196224864af220294345fd141.r2.dev/";
  var POLL_MS = 15000;
  var DATASET_POLL_MS = 60000;
  var lastPayload = null;
  var historyShowErrors = false;
  var smoothMode = localStorage.getItem("smoothMode") || "lowess";
  if (smoothMode !== "lowess" && smoothMode !== "normal") smoothMode = "lowess";
  function el(id) { return document.getElementById(id); }
  function text(id, value) { el(id).textContent = value == null || value === "" ? "--" : String(value); }
  function finite(value) { if (value == null || value === "") return null; var n = Number(value); return Number.isFinite(n) ? n : null; }
  function number(value, digits) { var n = finite(value); return n == null ? "--" : n.toLocaleString(undefined, { minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 }); }
  function metric(value, digits) { var n = finite(value); return n == null ? "--" : n.toFixed(digits == null ? 6 : digits); }
  function percent(value) { var n = finite(value); return n == null ? "--" : (n * 100).toFixed(1) + "%"; }
  function usd(value) { var n = finite(value); return n == null ? "--" : "$" + n.toFixed(2); }
  function short(value, head, tail) { var s = String(value || ""); head = head || 9; tail = tail || 5; return s.length > head + tail + 1 ? s.slice(0, head) + "…" + s.slice(-tail) : (s || "--"); }
  function date(value) { if (!value) return "--"; var d = new Date(value); return Number.isNaN(d.getTime()) ? "--" : d.toLocaleString([], { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
  function age(value) { if (!value) return "--"; var seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000)); if (seconds < 60) return seconds + "S AGO"; if (seconds < 3600) return Math.floor(seconds / 60) + "M AGO"; if (seconds < 86400) return Math.floor(seconds / 3600) + "H AGO"; return Math.floor(seconds / 86400) + "D AGO"; }
  function identity(row) { if (!row) return "--"; if (row.model_identity === "hidden_until_promotion") return TeutonicDashboardV1.HIDDEN; return row.challenger_repo || row.model_repo || (row.model_digest ? "SHA256 " + short(row.model_digest, 10, 4) : "PUBLIC MODEL"); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function cell(row, value, className, title) { var td = document.createElement("td"); td.textContent = value == null ? "--" : value; if (className) td.className = className; if (title) td.title = title; row.appendChild(td); return td; }
  function hotkeyLink(hotkey, head, tail) { var value = String(hotkey || ""), href = TeutonicDashboardV1.taoMarketCapHotkeyUrl(value), node = document.createElement(href ? "a" : "span"); node.textContent = short(value, head, tail); node.title = value ? "Open " + value + " on Tao Market Cap" : ""; if (href) { node.href = href; node.target = "_blank"; node.rel = "noopener"; } return node; }
  function hotkeyCell(row, hotkey) { var td = cell(row, "", "mono", hotkey); td.appendChild(hotkeyLink(hotkey, 12, 6)); return td; }
  function hotkeyText(id, prefix, hotkey, head, tail) { var node = el(id); clear(node); node.title = hotkey || ""; node.appendChild(document.createTextNode(prefix)); node.appendChild(hotkeyLink(hotkey, head, tail)); }
  function emptyRow(body, columns, message) { clear(body); var row = document.createElement("tr"); var td = cell(row, message, "empty-cell"); td.colSpan = columns; body.appendChild(row); }
  function historyShardRow(item, index) {
    var view = TeutonicDashboardV1.shardPresentation(item), row = document.createElement("tr"), td = cell(row, "", "history-shards-cell"), panel = document.createElement("div"), heading = document.createElement("strong");
    row.className = "history-shards-row"; row.id = "history-shards-" + String(item.challenge_id || "evaluation").replace(/[^a-zA-Z0-9_-]/g, "") + "-" + index; row.hidden = true; row.setAttribute("role", "region"); row.setAttribute("aria-label", "Dataset shards used for evaluation " + (item.challenge_id || index + 1)); td.colSpan = 9; panel.className = "history-shards-panel"; heading.textContent = "SHARDS USED · " + view.count; panel.appendChild(heading);
    if (!view.count) { var empty = document.createElement("p"); empty.className = "history-shards-empty"; empty.textContent = "SHARD DATA UNAVAILABLE FOR THIS EVALUATION"; panel.appendChild(empty); }
    view.groups.forEach(function (group) { var section = document.createElement("section"), label = document.createElement("h3"), list = document.createElement("ul"); label.textContent = group.source + " · " + group.names.length; group.names.forEach(function (name) { var entry = document.createElement("li"), code = document.createElement("code"); code.textContent = name; entry.appendChild(code); list.appendChild(entry); }); section.appendChild(label); section.appendChild(list); panel.appendChild(section); });
    td.appendChild(panel); return row;
  }
  function makeHistoryRowExpandable(row, details, item) {
    row.classList.add("history-row"); row.tabIndex = 0; row.setAttribute("aria-expanded", "false"); row.setAttribute("aria-controls", details.id); row.setAttribute("aria-label", "Show shards used for evaluation " + (item.challenge_id || "")); row.title = "Click to show dataset shards";
    function toggle(event) { if (event.type === "click" && event.target.closest("a,button")) return; if (event.type === "keydown" && event.key !== "Enter" && event.key !== " ") return; if (event.type === "keydown") event.preventDefault(); var expanded = row.getAttribute("aria-expanded") === "true"; row.setAttribute("aria-expanded", expanded ? "false" : "true"); row.setAttribute("aria-label", (expanded ? "Show" : "Hide") + " shards used for evaluation " + (item.challenge_id || "")); row.title = expanded ? "Click to show dataset shards" : "Click to hide dataset shards"; details.hidden = expanded; }
    row.addEventListener("click", toggle); row.addEventListener("keydown", toggle);
  }
  function ema(values, alpha) { if (alpha >= 1 || !values.length) return values.slice(); var output = [values[0]]; for (var i = 1; i < values.length; i++) output.push(alpha * values[i] + (1 - alpha) * output[i - 1]); return output; }
  function lowess(values, strength) { var n = values.length; if (n < 3 || strength <= 0) return values.slice(); var span = Math.min(n, Math.max(3, Math.ceil(n * (.12 + strength * .58)))), output = []; for (var i = 0; i < n; i++) { var distances = []; for (var j = 0; j < n; j++) distances.push({ index: j, distance: Math.abs(j - i) }); distances.sort(function (a, b) { return a.distance - b.distance; }); var bandwidth = distances[span - 1].distance || 1, sw = 0, swx = 0, swy = 0, swxx = 0, swxy = 0; for (var k = 0; k < span; k++) { var index = distances[k].index, u = distances[k].distance / bandwidth, weight = Math.pow(1 - Math.pow(u, 3), 3), xValue = index, yValue = values[index]; sw += weight; swx += weight * xValue; swy += weight * yValue; swxx += weight * xValue * xValue; swxy += weight * xValue * yValue; } var denominator = sw * swxx - swx * swx; output.push(Math.abs(denominator) < 1e-12 || sw === 0 ? (sw ? swy / sw : values[i]) : (swy - ((sw * swxy - swx * swy) / denominator) * swx) / sw + ((sw * swxy - swx * swy) / denominator) * i); } return output; }
  function smoothSeries(values, amount) { if (amount <= 0) return values.slice(); return smoothMode === "normal" ? ema(values, 1 / (1 + amount * 20)) : lowess(values, amount); }

  function compactNumber(value) {
    var n = finite(value); if (n == null) return "--";
    var abs = Math.abs(n), units = [{ value: 1e12, suffix: "T" }, { value: 1e9, suffix: "B" }, { value: 1e6, suffix: "M" }, { value: 1e3, suffix: "K" }];
    for (var i = 0; i < units.length; i++) { if (abs >= units[i].value) { var scaled = n / units[i].value; return scaled.toFixed(Math.abs(scaled) >= 100 ? 0 : 2).replace(/\.00$/, "") + units[i].suffix; } }
    return number(n);
  }
  function datasetWeight(value) { var n = finite(value); if (n == null) return "--"; var pct = n * 100; return (Math.abs(pct - Math.round(pct)) < .05 ? String(Math.round(pct)) : pct.toFixed(1)) + "%"; }
  function smallPercent(value) { var n = finite(value); if (n == null) return "--"; if (n === 0) return "0%"; if (Math.abs(n) < .01) return n.toPrecision(2) + "%"; return (Math.abs(n) >= 10 ? n.toFixed(1) : n.toFixed(3)).replace(/0+$/, "").replace(/\.$/, "") + "%"; }
  function revision(value) { var parts = String(value || "").split(":"); return parts.length === 2 ? parts[0] + ":" + parts[1].slice(0, 16) : short(value, 16, 0); }
  function compactTimestamp(value) { if (!value) return "--"; var d = new Date(value); return Number.isNaN(d.getTime()) ? "--" : d.toISOString().replace(/:\d{2}\.\d{3}Z$/, "Z"); }
  function setLink(id, label, href) { var node = el(id); node.textContent = label || "--"; node.href = href || "#"; if (href) { node.target = "_blank"; node.rel = "noopener"; } else { node.removeAttribute("target"); node.removeAttribute("rel"); } }
  function huggingFaceUrl(repo, digest) { var base = repo ? "https://huggingface.co/" + repo : ""; var raw = String(digest || "").replace(/^hf:/, ""); return base && raw ? base + "/tree/" + raw : base; }
  function datasetCell(row, value, subtext, href) {
    var td = document.createElement("td"), main;
    if (href) { main = document.createElement("a"); main.href = href; main.target = "_blank"; main.rel = "noopener"; main.textContent = value || "--"; td.appendChild(main); }
    else td.textContent = value || "--";
    if (subtext) { var sub = document.createElement("span"); sub.className = "dataset-sub"; sub.textContent = subtext; td.appendChild(sub); }
    row.appendChild(td);
  }
  async function fetchJson(url) {
    var separator = url.indexOf("?") === -1 ? "?" : "&";
    var response = await fetch(url + separator + "t=" + Date.now(), { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }
  async function fetchFirstJson(urls) {
    var lastError;
    for (var i = 0; i < urls.length; i++) { try { return await fetchJson(urls[i]); } catch (error) { lastError = error; } }
    throw lastError || new Error("no manifest endpoint available");
  }
  function renderDatasetManifest(manifest) {
    var view = TeutonicDashboardV1.datasetPresentation(manifest), summary = [view.rows.length + (view.rows.length === 1 ? " DATASET" : " DATASETS")];
    if (view.totalTokens) summary.push(compactNumber(view.totalTokens) + " TOKENS");
    if (view.totalShards) summary.push(number(view.totalShards) + " SHARDS");
    if (view.sequenceLength) summary.push("SEQ LEN " + number(view.sequenceLength));
    if (view.totalSequences) summary.push(compactNumber(view.totalSequences) + " POSSIBLE SEQ");
    if (view.evalN) summary.push("EVAL SAMPLE " + number(view.evalN) + " SEQ" + (view.evalTokens ? " / " + compactNumber(view.evalTokens) + " TOKENS" : ""));
    if (view.tokenizer) summary.push(view.tokenizer);
    text("dataset-summary", summary.join(" · "));
    var body = el("dataset-sources"); clear(body);
    if (!view.rows.length) return emptyRow(body, 6, "NO DATASETS IN MANIFEST");
    view.rows.forEach(function (source) {
      var tr = document.createElement("tr");
      datasetCell(tr, source.name, source.metadataLoaded ? source.tokenizationMode : "METADATA UNAVAILABLE", source.manifestUrl);
      datasetCell(tr, datasetWeight(source.weight), "NORMALIZED " + datasetWeight(source.normalizedWeight));
      datasetCell(tr, source.totalTokens ? compactNumber(source.totalTokens) : "--", source.totalShards ? number(source.totalShards) + " SHARDS" : "");
      datasetCell(tr, source.sequences ? compactNumber(source.sequences) : "--", source.sequenceLength ? "LEN " + number(source.sequenceLength) : "");
      datasetCell(tr, source.evalSequences ? number(source.evalSequences) + " SEQ" : "--", source.evalTokens ? compactNumber(source.evalTokens) + " TOKENS · " + smallPercent(source.sampleRate) + " SAMPLED" : "");
      datasetCell(tr, source.source || "--", [source.tokenizer, source.dtype].filter(Boolean).join(" · "));
      body.appendChild(tr);
    });
  }
  async function loadDatasetManifest() {
    try {
      var manifest = await fetchFirstJson(["/datasets/manifest.json", DATASET_MANIFEST_URL]);
      renderDatasetManifest(manifest);
    } catch (error) {
      text("dataset-summary", "MANIFEST UNAVAILABLE · USE THE MANIFEST LINK");
      emptyRow(el("dataset-sources"), 6, "DATASET MANIFEST COULD NOT BE LOADED");
    }
  }

  function renderHeader(d) {
    var chain = d.chain || {}, king = d.king || {}, market = d.market || {};
    text("tao-price", finite(market.tao_price_usd) == null ? "--" : usd(market.tao_price_usd));
    var change = finite(market.tao_change_24h), changeNode = el("tao-change");
    changeNode.textContent = change == null ? "" : (change > 0 ? "+" : "") + change.toFixed(1) + "%";
    changeNode.className = change == null ? "" : (change >= 0 ? "is-up" : "is-down");
    text("sn3-alpha", finite(market.sn3_alpha_price_tao) == null ? "--" : metric(market.sn3_alpha_price_tao, 4) + " τ");
    text("sn3-reg", finite(market.sn3_reg_burn_tao) == null ? "--" : metric(market.sn3_reg_burn_tao, 6) + " τ");

    var genesisRepo = chain.seed_repo, genesisDigest = chain.seed_digest;
    var genesisUrl = chain.seed_repo_backend === "hf" ? huggingFaceUrl(genesisRepo, genesisDigest) : "";
    setLink("genesis-link", genesisRepo, genesisUrl);
    setLink("genesis-revision", revision(genesisDigest), genesisUrl);

    var genesisKing = Number(king.reign_number) === 0 || !king.model_repo;
    var kingRepo = genesisKing ? genesisRepo : king.model_repo;
    var kingDigest = genesisKing ? genesisDigest : (king.king_digest || king.model_digest);
    var kingUrl = genesisKing ? genesisUrl : (king.model_reference ? new URL(king.model_reference + "manifest.json", MODEL_STORAGE_BASE).href : "");
    setLink("king-link", kingRepo, kingUrl);
    setLink("king-revision", revision(kingDigest), kingUrl);
    el("king-health").classList.toggle("is-live", !!d.king);
    text("king-reign", "REIGN " + (d.king ? "#" + number(king.reign_number) + " — " + compactTimestamp(king.crowned_at) : "--"));
    text("source-watermark", "WATERMARK " + number(d.source_watermark));
    document.title = (chain.name || "Teutonic") + " — Dashboard";
  }
  function renderEvaluation(d) {
    var ev = d.current_eval, service = d.service_status || {}, card = el("eval-card"); text("validator-phase", "VALIDATOR " + String(service.validator_phase || "--").toUpperCase());
    if (!ev) { card.dataset.active = "false"; text("eval-title", d.queue && d.queue.length ? "NEXT CHALLENGE QUEUED" : "NO ACTIVE CHALLENGE"); el("eval-title").title = d.queue && d.queue.length ? d.queue[0].hotkey || "" : ""; if (d.queue && d.queue.length) hotkeyText("eval-meta", "HOTKEY ", d.queue[0].hotkey, 16, 8); else text("eval-meta", "THE VALIDATOR IS READY FOR THE NEXT MODEL"); text("eval-stage", "WAITING"); text("eval-percent", "0%"); el("eval-progress").style.width = "0%"; return; }
    var pct = finite(ev.percent); if (pct == null && finite(ev.total) > 0) pct = finite(ev.progress) / finite(ev.total) * 100; pct = Math.max(0, Math.min(100, pct || 0));
    card.dataset.active = "true"; hotkeyText("eval-title", "HOTKEY ", ev.hotkey, 16, 8); text("eval-meta", "UID " + ev.uid + " · CHALLENGE " + ev.challenge_id + " · " + number(ev.elapsed_seconds) + "S ELAPSED"); text("eval-stage", String(ev.stage || "PROCESSING").replaceAll("_", " ")); text("eval-percent", pct.toFixed(0) + "% · " + number(ev.progress) + "/" + number(ev.total)); el("eval-progress").style.width = pct + "%";
  }
  function renderQueue(d) {
    var body = el("queue-body"), rows = d.queue || []; text("queue-count", rows.length + (rows.length === 1 ? " MODEL" : " MODELS")); if (!rows.length) return emptyRow(body, 7, "QUEUE EMPTY — VALIDATOR READY"); clear(body);
    rows.forEach(function (item, index) { var tr = document.createElement("tr"); cell(tr, "#" + (item.queue_position || index + 1)); cell(tr, item.uid); cell(tr, item.challenge_id, "mono"); hotkeyCell(tr, item.hotkey); cell(tr, number(item.block)); cell(tr, String(item.state || "queued").toUpperCase()); cell(tr, date(item.submitted_at)); body.appendChild(tr); });
  }
  function renderHistory(d) {
    var body = el("history-body"), view = TeutonicDashboardV1.historyPresentation(d.history || [], historyShowErrors), rows = view.rows.slice().sort(function (a, b) { return new Date(b.timestamp || 0) - new Date(a.timestamp || 0); });
    var countLabel = rows.length + (rows.length === 1 ? " RESULT" : " RESULTS");
    if (!historyShowErrors && view.errorCount) countLabel += " · " + view.errorCount + (view.errorCount === 1 ? " ERROR HIDDEN" : " ERRORS HIDDEN");
    text("history-count", countLabel);
    var toggle = el("history-errors-toggle"); toggle.textContent = historyShowErrors ? "HIDE ERRORS" : "SHOW ERRORS"; toggle.setAttribute("aria-pressed", historyShowErrors ? "true" : "false");
    if (!rows.length) return emptyRow(body, 9, view.errorCount && !historyShowErrors ? "NO NON-ERROR EVALUATIONS — ERRORS HIDDEN" : "NO EVALUATIONS YET"); clear(body);
    rows.forEach(function (item, index) { var tr = document.createElement("tr"), details = historyShardRow(item, index); cell(tr, item.uid); cell(tr, identity(item), "", item.challenger_repo || item.challenge_id); hotkeyCell(tr, item.hotkey); cell(tr, String(item.verdict || "--").toUpperCase(), "verdict " + (item.verdict || ""), item.error_message); cell(tr, metric(item.mu_hat)); cell(tr, metric(item.lcb)); cell(tr, metric(item.avg_king_loss, 4)); cell(tr, metric(item.avg_challenger_loss, 4)); var when = age(item.timestamp) + (finite(item.wall_time_s) == null ? "" : " (" + metric(item.wall_time_s, 0) + "S)"); cell(tr, when, "", date(item.timestamp)); makeHistoryRowExpandable(tr, details, item); body.appendChild(tr); body.appendChild(details); });
  }
  function renderReigns(d) {
    var body = el("reigns-body"), allRows = (d.king_chain || []).slice().sort(function (a, b) { return (b.reign_number || 0) - (a.reign_number || 0); }), seenHotkeys = {}, rows = [];
    allRows.forEach(function (item) { var key = item.hotkey || "reign:" + item.reign_number; if (rows.length < 5 && !seenHotkeys[key]) { seenHotkeys[key] = true; rows.push(item); } });
    text("reign-count", rows.length + " / 5 KINGS · " + allRows.length + " TOTAL REIGNS"); if (!rows.length) return emptyRow(body, 9, "NO REIGNS YET"); clear(body);
    rows.forEach(function (item, index) { var tr = document.createElement("tr"); cell(tr, index + 1); cell(tr, "#" + number(item.reign_number)); cell(tr, item.uid); hotkeyCell(tr, item.hotkey); var modelCell = cell(tr, identity(item), "", item.model_digest); if (item.model_reference) { var link = document.createElement("a"); link.href = new URL(item.model_reference + "manifest.json", MODEL_STORAGE_BASE).href; link.target = "_blank"; link.rel = "noopener"; link.textContent = identity(item); link.title = "Open model manifest"; modelCell.textContent = ""; modelCell.appendChild(link); } cell(tr, percent(item.weight)); cell(tr, metric(item.alpha_per_hour, 3)); cell(tr, usd(item.usd_per_hour)); cell(tr, date(item.crowned_at)); body.appendChild(tr); });
  }
  function renderWeightStatus(d) {
    var weight = d.weight_status || {}; text("weight-state", String(weight.state || weight.latest_attempt_state || "NOT SCHEDULED").toUpperCase()); text("weight-block", number(weight.latest_finalized_block || weight.last_attempted_block)); text("weight-next", number(weight.next_due_block)); text("weight-finalized", date(weight.finalized_at));
  }
  function renderChart(d) {
    var svg = el("loss-chart");
    var amount = finite(el("smooth-slider").value) || 0;
    var smoothLabel = amount > 0 ? "SMOOTH " + smoothMode.toUpperCase() + " " + amount.toFixed(2) : "SMOOTH OFF";
    text("smooth-status", smoothLabel);
    var points = (d.history || []).filter(function (item) { return finite(item.avg_king_loss) != null && finite(item.avg_challenger_loss) != null; }).sort(function (a, b) { return new Date(a.timestamp || 0) - new Date(b.timestamp || 0); });
    text("chart-count", points.length + (points.length === 1 ? " EVALUATION" : " EVALUATIONS"));
    el("chart-empty").hidden = points.length > 0;
    if (!points.length) { svg.innerHTML = ""; return; }

    var W = 1000, H = 280, left = 55, right = 15, top = 15, bottom = 30;
    var kingPoints = [];
    var rawChallengers = points.map(function (p) { return finite(p.avg_challenger_loss); });
    points.forEach(function (p, i) { if (p.accepted && p.publication_disposition === "winner" && p.model_identity === "public") kingPoints.push({ index: i, loss: finite(p.avg_challenger_loss) }); });
    var rawKings = kingPoints.map(function (p) { return p.loss; });
    var challengers = smoothSeries(rawChallengers, amount);
    var kings = smoothSeries(rawKings, amount);
    var values = rawChallengers.concat(rawKings, challengers, kings);
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values), padding = Math.max((max - min) * .2, .001); min -= padding; max += padding;

    function x(i) { return points.length === 1 ? (left + W - right) / 2 : left + i / (points.length - 1) * (W - left - right); }
    function y(v) { return top + (max - v) / (max - min) * (H - top - bottom); }
    function challengerLine(series) { return series.map(function (value, i) { return x(i).toFixed(1) + "," + y(value).toFixed(1); }).join(" "); }
    function kingLine(series) { return series.map(function (value, i) { return x(kingPoints[i].index).toFixed(1) + "," + y(value).toFixed(1); }).join(" "); }

    var styles = getComputedStyle(document.documentElement), ink = styles.getPropertyValue("--ink").trim(), muted = styles.getPropertyValue("--muted").trim(), paper = styles.getPropertyValue("--paper").trim(), markup = "";
    for (var tick = 0; tick <= 4; tick++) { var value = min + (max - min) * tick / 4, yy = y(value); markup += '<line x1="' + left + '" y1="' + yy + '" x2="' + (W - right) + '" y2="' + yy + '" stroke="' + muted + '" opacity=".22" stroke-dasharray="3 6"/><text x="' + (left - 8) + '" y="' + (yy + 3) + '" fill="' + muted + '" font-family="Space Mono" font-size="9" text-anchor="end">' + value.toFixed(4) + '</text>'; }
    if (amount > 0 && points.length > 1) markup += '<polyline points="' + challengerLine(rawChallengers) + '" fill="none" stroke="' + muted + '" stroke-width="1" opacity=".24" stroke-dasharray="2 5"/>';
    if (amount > 0 && kingPoints.length > 1) markup += '<polyline points="' + kingLine(rawKings) + '" fill="none" stroke="' + ink + '" stroke-width="1" opacity=".2" stroke-dasharray="2 5"/>';
    if (points.length > 1) markup += '<polyline points="' + challengerLine(challengers) + '" fill="none" stroke="' + muted + '" stroke-width="1.5" stroke-dasharray="6 5"/>';
    if (kingPoints.length > 1) markup += '<polyline points="' + kingLine(kings) + '" fill="none" stroke="' + ink + '" stroke-width="2"/>';
    rawChallengers.forEach(function (value, i) { markup += '<circle cx="' + x(i) + '" cy="' + y(value) + '" r="2.5" fill="' + paper + '" stroke="' + muted + '"/>'; });
    kingPoints.forEach(function (p) { markup += '<circle cx="' + x(p.index) + '" cy="' + y(p.loss) + '" r="3" fill="' + ink + '"/>'; });
    svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.innerHTML = markup;
  }
  function render(d) { TeutonicDashboardV1.validate(d); lastPayload = d; renderHeader(d); renderReigns(d); renderEvaluation(d); renderChart(d); renderQueue(d); renderHistory(d); renderWeightStatus(d); text("last-refresh", "LAST REFRESH " + new Date().toLocaleTimeString()); el("error-banner").hidden = true; }
  async function poll() { try { var response = await fetch(ENDPOINT + "?t=" + Date.now(), { cache: "no-store" }); if (!response.ok) throw new Error("dashboard request returned HTTP " + response.status); render(await response.json()); } catch (error) { var banner = el("error-banner"); banner.textContent = "DATA REFRESH FAILED — " + error.message + (lastPayload ? " — SHOWING LAST GOOD PUBLICATION" : ""); banner.hidden = false; } }
  function setTheme(theme) { document.documentElement.dataset.theme = theme; el("theme-toggle").textContent = theme === "dark" ? "LIGHT" : "DARK"; if (lastPayload) renderChart(lastPayload); }
  var savedTheme = localStorage.getItem("dashboard-theme"); setTheme(savedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")); el("theme-toggle").addEventListener("click", function () { var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; localStorage.setItem("dashboard-theme", next); setTheme(next); });
  var smoothSlider = el("smooth-slider"), savedSmoothing = localStorage.getItem("smoothing");
  if (savedSmoothing != null && finite(savedSmoothing) != null) smoothSlider.value = savedSmoothing;
  function updateSmoothControls() { var value = Number(smoothSlider.value); text("smooth-value", value.toFixed(2).replace(/0$/, "").replace(/\.$/, "")); el("smooth-mode-toggle").textContent = smoothMode === "normal" ? "NORMAL" : "LOWESS"; el("smooth-mode-toggle").title = smoothMode === "normal" ? "Normal EMA smoothing; click for LOWESS" : "LOWESS smoothing; click for normal EMA"; }
  updateSmoothControls();
  smoothSlider.addEventListener("input", function () { localStorage.setItem("smoothing", smoothSlider.value); updateSmoothControls(); if (lastPayload) renderChart(lastPayload); });
  el("smooth-mode-toggle").addEventListener("click", function () { smoothMode = smoothMode === "lowess" ? "normal" : "lowess"; localStorage.setItem("smoothMode", smoothMode); updateSmoothControls(); if (lastPayload) renderChart(lastPayload); });
  el("history-errors-toggle").addEventListener("click", function () { historyShowErrors = !historyShowErrors; if (lastPayload) renderHistory(lastPayload); });
  poll(); setInterval(poll, POLL_MS);
  loadDatasetManifest(); setInterval(loadDatasetManifest, DATASET_POLL_MS);
})();
