"use strict";
// AWS Waste Finder frontend. No frameworks, no third-party scripts.
// All scan data is written with textContent, never innerHTML.

(() => {
  const $ = (sel) => document.querySelector(sel);
  const money = (n) => "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const BASIS = { "exact": "Exact", "estimate": "Estimate", "upper-bound": "Up to" };
  const POLL_MS = 4000;
  const POLL_LIMIT_MS = 12 * 60 * 1000;

  const state = { api: null, session: null, current: null, sample: null };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function getJSON(url, options) {
    const res = await fetch(url, options);
    let body = {};
    try { body = await res.json(); } catch (_) { /* empty or non-JSON body */ }
    if (!res.ok) {
      const err = new Error(body.error || `Request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  async function loadSample() {
    if (!state.sample) state.sample = await getJSON("sample-scan.json");
    return state.sample;
  }

  // ---------- receipt lines ----------

  function receiptLine(what, where, amount) {
    const li = el("li");
    li.append(el("span", "what", what), el("span", "amt", money(amount)));
    if (where) li.append(el("span", "where", where));
    return li;
  }

  function printReceipt(list, items) {
    list.replaceChildren(...items);
    const receipt = list.closest(".receipt");
    receipt.classList.remove("printing");
    void receipt.offsetWidth;  // restart the feed animation on re-render
    receipt.classList.add("printing");
  }

  function stamp(date) {
    const d = date ? new Date(date) : new Date();
    return d.toLocaleDateString("en-US", { month: "2-digit", day: "2-digit", year: "numeric" }) + " " +
      d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  }

  // Decorative barcode, deterministic for a given text.
  function barcode(container, text) {
    let h = 2166136261;
    const bars = [];
    for (let i = 0; i < 46; i++) {
      h = Math.imul(h ^ text.charCodeAt(i % text.length) ^ i, 16777619) >>> 0;
      const bar = el("span", i % 2 ? "gap" : null);
      bar.style.width = (1 + (h % 3)) + "px";
      bars.push(bar);
    }
    container.replaceChildren(...bars);
  }

  function maskedAccount(id) {
    return id ? "Acct: ****" + String(id).slice(-4) : "Acct: demo";
  }

  function renderHero(sample) {
    const top = sample.findings.slice(0, 6);
    const lines = top.map((f) =>
      receiptLine(f.title.replace(/s$/, ""), `${f.region}  ${f.resource_id}`, f.monthly_cost));
    const rest = sample.findings.length - top.length;
    if (rest > 0) lines.push(el("li", "more", `+ ${rest} more items`));
    printReceipt($("#hero-lines"), lines);
    $("#hero-date").textContent = stamp();
    $("#hero-count").textContent = String(sample.findings.length);
    $("#hero-regions").textContent = String((sample.regions || []).length);
    $("#hero-total").textContent = money(sample.monthly_total);
    $("#hero-annual").textContent = money(sample.annual_total);
    barcode($("#hero-barcode"), "WF-DEMO-2026");
  }

  // ---------- results ----------

  function renderResults(result, { isSample }) {
    state.current = result;
    const section = $("#results");
    section.hidden = false;

    const regions = (result.regions || []).length;
    const when = result.finished_at ? new Date(result.finished_at).toLocaleString() : "";
    $("#results-title").textContent = isSample ? "Sample scan results" : "Your scan results";
    $("#results-meta").textContent = isSample
      ? `A demo account with typical leftovers. ${regions} regions scanned.`
      : `Account ${result.account_id}. ${regions} regions scanned${when ? ", finished " + when : ""}.`;
    $("#results-account").textContent = isSample ? "Acct: demo" : maskedAccount(result.account_id);
    $("#results-date").textContent = stamp(result.finished_at);

    const summary = Object.entries(result.summary || {});
    const lines = summary.map(([, s]) => receiptLine(s.title, `qty ${s.count}`, s.monthly_cost));
    if (lines.length === 0) lines.push(el("li", "more", "No waste found. Nice."));
    printReceipt($("#category-lines"), lines);
    $("#results-count").textContent = String(result.findings.length);
    $("#results-regions").textContent = String(regions);
    $("#results-total").textContent = money(result.monthly_total);
    $("#results-annual").textContent = money(result.annual_total);
    const code = isSample ? "WF-DEMO-2026" : "WF-" + String(result.account_id).slice(-4) + "-" +
      (result.finished_at || "").slice(0, 10).replace(/-/g, "");
    $("#results-code").textContent = code;
    barcode($("#results-barcode"), code);

    const filter = $("#filter");
    filter.replaceChildren(el("option", null, "All findings"));
    filter.firstChild.value = "";
    summary.forEach(([id, s]) => {
      const opt = el("option", null, `${s.title} (${s.count})`);
      opt.value = id;
      filter.append(opt);
    });
    renderFindings();

    const notes = [...(result.notices || []),
      ...(result.errors || []).map((e) => `${e.check} in ${e.region}: ${e.message}`)];
    $("#scan-notes").hidden = notes.length === 0;
    $("#notes-list").replaceChildren(...notes.map((n) => el("li", null, n)));

    section.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    $("#results-title").setAttribute("tabindex", "-1");
    $("#results-title").focus({ preventScroll: true });
  }

  function renderFindings() {
    const result = state.current;
    if (!result) return;
    const chosen = $("#filter").value;
    const list = result.findings.filter((f) => !chosen || f.check === chosen);
    $("#finding-count").textContent = list.length === 1 ? "1 finding" : `${list.length} findings`;

    if (list.length === 0) {
      $("#finding-list").replaceChildren(el("li", "finding",
        "No waste found in this account. Run it again after your next cleanup or architecture change."));
      return;
    }
    $("#finding-list").replaceChildren(...list.map((f) => {
      const li = el("li", "finding");
      const top = el("div", "finding-top");
      const amt = el("span", "finding-amt", money(f.monthly_cost));
      amt.append(el("small", null, " /mo"));
      top.append(el("span", "finding-title", f.title), amt);
      li.append(
        top,
        el("div", "finding-id", `${f.region}  ${f.resource_type}  ${f.resource_id}`),
        el("p", null, f.detail),
        el("p", "fix", f.action),
        el("p", "basis", `${BASIS[f.confidence] || f.confidence}: ${money(f.monthly_cost)} per month`),
      );
      return li;
    }));
  }

  function downloadJSON() {
    if (!state.current) return;
    const blob = new Blob([JSON.stringify(state.current, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a");
    a.href = url;
    a.download = `aws-waste-${state.current.account_id}.json`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---------- live scan ----------

  function setStep(id, status) {
    const step = $(id);
    step.classList.toggle("is-active", status === "active");
    step.classList.toggle("is-done", status === "done");
  }

  function setStatus(text, isError) {
    const s = $("#scan-status");
    s.textContent = text;
    s.classList.toggle("is-error", Boolean(isError));
  }

  async function startSession(button) {
    button.disabled = true;
    button.textContent = "Creating link...";
    try {
      state.session = await getJSON(`${state.api}/sessions`, { method: "POST" });
      const link = $("#launch-link");
      link.href = state.session.launchStackUrl;
      $("#external-id").textContent = state.session.externalId;
      $("#session-detail").hidden = false;
      button.hidden = true;
      setStep("#step-role", "done");
      setStep("#step-scan", "active");
      $("#account-id").disabled = false;
      $("#scan-btn").disabled = false;
    } catch (err) {
      button.disabled = false;
      button.textContent = "Get my one-time link";
      setStatus(`Couldn't create a link: ${err.message}`, true);
    }
  }

  function cleanAccountId(raw) {
    return raw.replace(/[\s-]/g, "");
  }

  async function runScan(event) {
    event.preventDefault();
    const accountId = cleanAccountId($("#account-id").value);
    const error = $("#account-error");
    if (!/^\d{12}$/.test(accountId)) {
      error.textContent = "Enter the 12-digit account ID shown in the top-right menu of the AWS console.";
      $("#account-id").focus();
      return;
    }
    error.textContent = "";
    const btn = $("#scan-btn");
    btn.disabled = true;
    setStatus("Starting the scan...");
    try {
      const { scanId } = await getJSON(`${state.api}/scans`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: state.session.sessionId, accountId }),
      });
      await poll(scanId);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function poll(scanId) {
    const started = Date.now();
    while (Date.now() - started < POLL_LIMIT_MS) {
      const scan = await getJSON(`${state.api}/scans/${encodeURIComponent(scanId)}`, {
        headers: { "X-Session-Id": state.session.sessionId },
      });
      if (scan.status === "done") {
        const r = scan.result;
        setStatus(`Done. ${r.findings.length} findings, ${money(r.monthly_total)} a month.`);
        setStep("#step-scan", "done");
        setStep("#step-cleanup", "active");
        renderResults(r, { isSample: false });
        return;
      }
      if (scan.status === "failed") throw new Error(scan.error || "The scan failed.");
      const secs = Math.round((Date.now() - started) / 1000);
      setStatus(scan.status === "queued" ? "Queued..." : `Scanning every region... ${secs}s`);
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
    throw new Error("The scan is taking longer than expected. Try again in a few minutes.");
  }

  // ---------- wiring ----------

  async function init() {
    document.addEventListener("click", (e) => {
      const action = e.target.closest("[data-action]")?.dataset.action;
      if (action === "sample") loadSample().then((s) => renderResults(s, { isSample: true }));
      if (action === "session") startSession(e.target.closest("button"));
      if (action === "download") downloadJSON();
    });
    $("#filter").addEventListener("change", renderFindings);
    $("#scan-form").addEventListener("submit", runScan);

    loadSample().then(renderHero).catch(() => { $(".receipt-hero").hidden = true; });

    try {
      const cfg = await getJSON("config.json", { cache: "no-store" });
      const allowed = /^https:\/\//.test(cfg.apiUrl || "") ||
        /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/.test(cfg.apiUrl || "");  // local testing only
      if (allowed) state.api = cfg.apiUrl.replace(/\/$/, "");
    } catch (_) { /* no config: sample-only copy of the site */ }

    if (!state.api) {
      $("#scan-unavailable").hidden = false;
      $("#scan-steps").hidden = true;
    } else {
      setStep("#step-role", "active");
    }
  }

  init();
})();
