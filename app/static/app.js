/* Ledger Reconciliation - single-page frontend (no build step). */
"use strict";

const $app = document.getElementById("app");
const $user = document.getElementById("user");
const fmt = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
let pollTimer = null;
let ME = null;

// ------------------------------------------------------------------ helpers
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function money(v) {
  if (v === null || v === undefined) return "";
  const s = fmt.format(Math.abs(v));
  return v < 0 ? `<span class="neg">(${s})</span>` : s;
}
function dmy(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-");
  return `${d}-${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+m - 1]}-${y.slice(2)}`;
}
function badge(conf) {
  const label = { green: "Proven", amber: "Partly proven", red: "Not proven" }[conf] || conf;
  return `<span class="badge ${esc(conf)}">${esc(label)}</span>`;
}
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) { renderSignIn(); throw new Error("signin"); }
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || res.statusText);
  return body;
}
function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

// ------------------------------------------------------------------ routing
window.addEventListener("hashchange", route);
async function jpost(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}
async function route() {
  stopPolling();
  if (!ME) {
    try {
      const r = await fetch("/api/me", { credentials: "same-origin" });
      if (r.status === 401) return renderSignIn();
      if (!r.ok) { $app.innerHTML = `<div class="error">The server had a problem (${r.status}). Reload the page; if it keeps happening, sign out and in again.</div>`; return; }
      ME = await r.json();
    } catch (e) { $app.innerHTML = `<div class="error">Cannot reach the server.</div>`; return; }
  }
  const u = ME.user;
  $user.innerHTML = `${esc(u.name || u.username)}${ME.dev_mode ? ' <span class="badge amber">dev mode</span>' : ""}
    ${u.is_admin ? '<a href="#/users">Users</a>' : ""}
    ${ME.dev_mode ? "" : '<a href="#/password">Change password</a> <a href="#" id="signout">Sign out</a>'}`;
  const so = document.getElementById("signout");
  if (so) so.onclick = async ev => { ev.preventDefault(); await fetch("/auth/logout", { method: "POST" }); ME = null; renderSignIn(); };
  if (u.must_change && !ME.dev_mode) return renderPassword(true);
  if (location.hash.startsWith("#/users")) return renderUsers();
  if (location.hash.startsWith("#/password")) return renderPassword(false);
  const m = location.hash.match(/^#\/run\/([\w-]+)/);
  if (m) return renderRun(m[1]);
  const mc = location.hash.match(/^#\/check\/([\w-]+)/);
  if (mc) return renderCheck(mc[1]);
  if (location.hash.startsWith("#/payments")) return renderPayHome();
  return renderHome();
}

function renderSignIn() {
  stopPolling();
  ME = null;
  $user.innerHTML = "";
  $app.innerHTML = `<div class="panel" style="max-width:420px;margin:60px auto">
    <h1>Ledger Reconciliation</h1><p class="muted">Sign in with the login ID and password your administrator gave you.</p>
    <form id="login"><label for="lu">Login ID</label><input type="text" id="lu" autocomplete="username" required>
      <label for="lp">Password</label><input type="password" id="lp" autocomplete="current-password" required>
      <div class="row" style="margin-top:14px"><button type="submit">Sign in</button><span id="lmsg" class="neg small"></span></div></form></div>`;
  document.getElementById("login").onsubmit = async ev => {
    ev.preventDefault();
    const msg = document.getElementById("lmsg");
    msg.textContent = "";
    const r = await fetch("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("lu").value, password: document.getElementById("lp").value }) });
    if (!r.ok) { msg.textContent = (await r.json()).detail || "Sign-in failed"; return; }
    ME = null;
    route();
  };
}

function renderPassword(forced) {
  $app.innerHTML = `<div class="panel" style="max-width:460px;margin:30px auto"><h1>${forced ? "Set your password" : "Change password"}</h1>
    ${forced ? '<p class="muted">You signed in with a temporary password. Choose your own to continue.</p>' : ""}
    <form id="pw"><label for="po">Current password</label><input type="password" id="po" autocomplete="current-password" required>
      <label for="pn">New password</label><input type="password" id="pn" autocomplete="new-password" required>
      <div class="small muted">At least 10 characters, with upper- and lower-case letters and a digit.</div>
      <label for="pc">Repeat new password</label><input type="password" id="pc" autocomplete="new-password" required>
      <div class="row" style="margin-top:14px"><button type="submit">Save password</button><span id="pmsg" class="small"></span></div></form></div>`;
  document.getElementById("pw").onsubmit = async ev => {
    ev.preventDefault();
    const msg = document.getElementById("pmsg"), n = document.getElementById("pn").value;
    if (n !== document.getElementById("pc").value) { msg.innerHTML = '<span class="neg">The new passwords do not match.</span>'; return; }
    try {
      await jpost("/api/me/password", { old_password: document.getElementById("po").value, new_password: n });
      ME = null; location.hash = "#/"; route();
    } catch (e) { if (e.message !== "signin") msg.innerHTML = `<span class="neg">${esc(e.message)}</span>`; }
  };
}

async function renderUsers() {
  let users;
  try { users = await api("/api/users"); } catch (e) { if (e.message !== "signin") $app.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  $app.innerHTML = `<a href="#/">← All reconciliations</a><h1>Users</h1>
    <div class="panel"><h2>Add a user</h2><form id="nu" class="grid2">
      <div><label for="nuid">Login ID</label><input type="text" id="nuid" placeholder="e.g. priya or priya@company.com" required></div>
      <div><label for="nun">Name</label><input type="text" id="nun"></div>
      <div class="row"><label style="margin:0"><input type="checkbox" id="nua"> Administrator</label></div>
      <div class="row"><button type="submit">Add user</button></div></form><div id="numsg"></div></div>
    <div class="panel"><table><thead><tr><th>Login ID</th><th>Name</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead><tbody>
    ${users.map(x => `<tr><td>${esc(x.username)}</td><td>${esc(x.name)}</td><td>${x.is_admin ? "Administrator" : "User"}</td>
      <td>${x.active ? (x.must_change ? '<span class="badge amber">Temporary password</span>' : '<span class="badge green">Active</span>') : '<span class="badge grey">Deactivated</span>'}</td>
      <td class="small">${esc((x.last_login || "").slice(0, 16).replace("T", " "))}</td>
      <td><button class="secondary reset" data-id="${x.id}">Reset password</button>
        <button class="secondary toggle" data-id="${x.id}" data-active="${x.active}">${x.active ? "Deactivate" : "Activate"}</button></td></tr>`).join("")}
    </tbody></table></div>`;
  const showTemp = (r, what) => {
    document.getElementById("numsg").innerHTML = `<div class="info">${what} <strong>${esc(r.user.username)}</strong>. Temporary password (shown once):
      <code>${esc(r.temporary_password)}</code>. Share it privately; they must change it at first sign-in.</div>`;
  };
  document.getElementById("nu").onsubmit = async ev => {
    ev.preventDefault();
    try {
      const r = await jpost("/api/users", { username: document.getElementById("nuid").value, name: document.getElementById("nun").value,
        is_admin: document.getElementById("nua").checked });
      await renderUsers(); showTemp(r, "Added");
    } catch (e) { if (e.message !== "signin") document.getElementById("numsg").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  };
  document.querySelectorAll(".reset").forEach(b => b.onclick = async () => {
    if (!confirm("Reset this user's password? They will be signed out.")) return;
    const r = await jpost(`/api/users/${b.dataset.id}/reset`); await renderUsers(); showTemp(r, "New temporary password for");
  });
  document.querySelectorAll(".toggle").forEach(b => b.onclick = async () => {
    try {
      await api(`/api/users/${b.dataset.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: b.dataset.active !== "true" }) });
      renderUsers();
    } catch (e) { if (e.message !== "signin") alert(e.message); }
  });
}

// ------------------------------------------------------------------ home
async function renderHome() {
  $app.innerHTML = `${featureTabs("recon")}
  <p class="muted">Upload Walkover's ledger and the counterparty's ledger. The tool reads both, shows how it understood them, then reconciles.</p>
  <div class="panel">
    <h2>New reconciliation</h2>
    <form id="newform">
      <label for="cp">Counterparty</label>
      <input type="text" id="cp" name="counterparty" placeholder="e.g. Acme Technologies Pvt Ltd" required>
      <div class="grid2">
        <div><label for="wf">Walkover's ledger</label><input type="file" id="wf" name="walkover_file" accept=".xlsx,.xls,.xlsm,.csv,.pdf">
          <div class="small muted" style="margin:6px 0 2px">or paste a Google Sheets link</div>
          <input type="text" name="walkover_url" placeholder="https://docs.google.com/spreadsheets/d/…"></div>
        <div><label for="cf">Counterparty's ledger</label><input type="file" id="cf" name="counterparty_file" accept=".xlsx,.xls,.xlsm,.csv,.pdf">
          <div class="small muted" style="margin:6px 0 2px">or paste a Google Sheets link</div>
          <input type="text" name="counterparty_url" placeholder="https://docs.google.com/spreadsheets/d/…"></div>
      </div>
      <p class="small muted">Files: Excel, CSV or PDF. A PDF must be the one exported from the accounting software (Tally, SAP, a portal), not a scan. A Google Sheet must be shared as "Anyone with the link can view". Any column layout works: the tool works out the columns and checks them against the sheet's own totals.</p>
      <div class="row" style="margin-top:14px"><button type="submit" id="go">Extract with AI</button><span id="formmsg" class="muted"></span></div>
    </form>
  </div>
  <div class="panel"><h2>Recent</h2><div id="recent" class="muted">Loading…</div></div>`;
  document.getElementById("newform").addEventListener("submit", async ev => {
    ev.preventDefault();
    const btn = document.getElementById("go"), msg = document.getElementById("formmsg");
    const fd = new FormData(ev.target);
    for (const side of ["walkover", "counterparty"]) {          // empty file inputs would be sent as blank files
      const f = fd.get(`${side}_file`);
      if (f && !f.name) fd.delete(`${side}_file`);
    }
    btn.disabled = true; msg.innerHTML = '<span class="spinner"></span>Uploading…';
    try {
      const r = await api("/api/runs", { method: "POST", body: fd });
      location.hash = `#/run/${r.id}`;
    } catch (e) { if (e.message !== "signin") { msg.innerHTML = `<span class="neg">${esc(e.message)}</span>`; btn.disabled = false; } }
  });
  try {
    const runs = await api("/api/runs");
    const el = document.getElementById("recent");
    if (!runs.length) { el.textContent = "No reconciliations yet."; return; }
    el.classList.remove("muted");
    el.innerHTML = `<table><thead><tr><th>Counterparty</th><th>Files</th><th>Started</th><th>By</th><th>Status</th><th class="num">Unexplained</th></tr></thead><tbody>
      ${runs.map(r => `<tr><td><a href="#/run/${esc(r.id)}">${esc(r.counterparty)}</a></td>
        <td class="small">${esc(r.w_filename)}<br>${esc(r.c_filename)}</td>
        <td>${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</td><td class="small">${esc(r.created_by)}</td>
        <td>${statusBadge(r.status)}</td><td class="num">${r.residual === null || r.residual === undefined ? "" : money(r.residual)}</td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) { /* handled */ }
}
function featureTabs(active) {
  return `<div class="features">
    <a class="feature ${active === "recon" ? "active" : ""}" href="#/"><strong>Two-party reconciliation</strong>
      <span class="small">Walkover's ledger against the counterparty's</span></a>
    <a class="feature ${active === "pay" ? "active" : ""}" href="#/payments"><strong>Payment check</strong>
      <span class="small">Walkover's ledger only: each invoice against the payment received</span></a>
  </div>`;
}
function statusBadge(s) {
  const map = { parsing: ["grey", "Reading"], parsed: ["amber", "Check columns"], reconciling: ["grey", "Reconciling"],
                done: ["green", "Done"], failed: ["red", "Failed"], uploaded: ["grey", "Uploaded"] };
  const [c, l] = map[s] || ["grey", s];
  return `<span class="badge ${c}">${esc(l)}</span>`;
}

// ------------------------------------------------------------------ run page
const VIEW = { tab: "matches", tier: "", bucket: "", q: "", showCheck: false };

async function renderRun(id) {
  let run;
  try { run = await api(`/api/runs/${id}`); } catch (e) { if (e.message !== "signin") $app.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  const busy = run.status === "parsing" || run.status === "reconciling";
  const head = `<div class="row"><div><a href="#/">← All reconciliations</a><h1>${esc(run.counterparty)}</h1>
      <div class="muted small">Started ${esc((run.created_at || "").slice(0, 16).replace("T", " "))} by ${esc(run.created_by)}</div></div>
      <div class="spacer"></div>${run.status === "parsed" && run.preview && run.preview.W && run.preview.C
        && run.preview.W.confidence === "green" && run.preview.C.confidence === "green"
        ? '<span class="badge green">Ready to reconcile</span>' : statusBadge(run.status)}</div>
    <div class="steps"><span class="step ${run.status === "done" ? "done" : "active"}">1. Check how the ledgers were read</span>
      <span class="step ${run.status === "done" ? "active" : ""}">2. Results</span></div>`;
  if (busy) {
    const what = run.status === "parsing" ? "AI is reading both workbooks; code is copying the amounts and checking them against the files' own totals…" : "Reconciling with maths: matching entries and building the bridge…";
    $app.innerHTML = head + `<div class="panel"><span class="spinner"></span>${what}</div>`;
    pollTimer = setTimeout(() => renderRun(id), 1500);
    return;
  }
  let html = head;
  if (run.error) html += `<div class="error">${esc(run.error)}</div>`;
  if (run.status === "done" && run.result && !VIEW.showCheck) {
    html += resultsView(run);
  } else if (run.preview && run.preview.W && run.preview.C) {
    html += checkView(run);
  }
  $app.innerHTML = html;
  bindRun(run);
}

// ---------- step 1: check columns
function checkView(run) {
  const P = run.preview;
  const ready = P.W.confidence !== "red" && P.C.confidence !== "red";
  const bothProven = P.W.confidence === "green" && P.C.confidence === "green";
  return `
  ${bothProven ? `<div class="info">Both ledgers were read and proven against their own totals and balances. Press Reconcile to match them.</div>`
    : P.W.extraction === "ai" ? `<div class="info">AI has read both workbooks: it chose the sheet, the table and the columns, and labelled every row (invoice, payment, TDS, ...). Every amount and date was copied from the cells by code, not typed by AI. Check the extraction below; change a column if it is wrong. When you press Reconcile, only maths is used.</div>`
    : `<div class="info">AI extraction is switched off on this server (no API key), so the ledgers were read with rules. Check the extraction below; change a column if it is wrong. When you press Reconcile, only maths is used.</div>`}
  ${bothProven ? "" : aiExtractLog(P.ai)}
  <div class="grid2">${sidePanel(run, "W", "Walkover's ledger", run.w_filename)}${sidePanel(run, "C", `${run.counterparty}'s ledger`, run.c_filename)}</div>
  <div class="panel row"><div><strong>Ready to reconcile?</strong>
    <div class="muted small">${ready ? "Both ledgers were proven against their own numbers." : "A ledger could not be proven. You can still reconcile, but check its columns first."}</div></div>
    <div class="spacer"></div>
    ${run.status === "done" ? '<button class="secondary" id="toresults">Back to results</button>' : ""}
    <button id="reconcile">${run.status === "done" ? "Reconcile again" : "Reconcile"}</button></div>`;
}

function aiExtractLog(A) {
  if (!A || !A.calls) return "";
  return `<details class="panel"><summary>AI extraction: ${A.calls} request(s)${A.rejected ? `, ${A.rejected} invoice reference(s) ignored because they were not in the row` : ""}${A.failures ? `, ${A.failures} failed` : ""}</summary>
    <ul class="small">${A.log.map(l => `<li>${esc(l)}</li>`).join("")}</ul></details>`;
}

function sidePanel(run, side, title, filename) {
  const p = run.preview[side];
  if (p.confidence === "green") return provenPanel(p, title, filename);
  const choices = run.role_choices;
  const sheetSel = p.sheets.length > 1 ? `<label>Sheet</label><select class="sheet" data-side="${side}">
      ${p.sheets.map(s => `<option ${s === p.sheet ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>` : "";
  const tables = p.tables.map(t => `
    <h3>Columns ${p.tables.length > 1 ? `(table ${t.index + 1}, header row ${t.header_row})` : `(header row ${t.header_row})`}
      ${t.known_format ? '<span class="chip">known format</span>' : ""}${t.user_mapping ? '<span class="chip">set by you</span>' : ""}</h3>
    <div class="scroll" style="max-height:320px"><table><thead><tr><th>Column</th><th>Sample values</th><th>Read as</th></tr></thead><tbody>
    ${t.columns.map(c => `<tr><td><span class="chip">${esc(c.letter)}</span> ${esc(c.label) || '<span class="muted">(no header)</span>'}</td>
      <td class="small muted">${c.samples.map(esc).join(" · ")}</td>
      <td>${c.helper ? '<span class="muted small">helper column (ignored)</span>' :
        `<select class="role" data-side="${side}" data-table="${t.index}" data-col="${c.col}" data-orig="${esc(c.role)}">
          <option value="">— not used —</option>${choices.map(o => `<option value="${o.value}" ${o.value === c.role ? "selected" : ""}>${esc(o.label)}</option>`).join("")}
        </select>`}</td></tr>`).join("")}
    </tbody></table></div>`).join("");
  return `<div class="panel">
    <div class="row"><h2 style="margin:0">${esc(title)}</h2><div class="spacer"></div>
      <span class="badge ${p.extraction === "rules" ? "grey" : "green"}">${p.extraction === "vision" ? "Read from page images" : p.extraction === "ai" ? "Extracted by AI" : "Extracted by rules"}</span>${badge(p.confidence)}</div>
    <div class="small muted">Types: ${Object.entries(p.kinds || {}).map(([k, n]) => `${esc(k)} ${n}`).join(" · ")}</div>
    <div class="muted small">${esc(filename)} · ${p.entries} entries · ${dmy(p.first_date)} to ${dmy(p.last_date)}${p.party_account ? ` · party account: ${esc(p.party_account)}` : ""}</div>
    ${sheetSel}
    <h3>Cross-check with the file's own totals</h3>
    ${p.checks.length ? p.checks.map(c => `<div class="check ${c.ok ? "ok" : c.informational ? "note" : "no"}"><span class="mark">${c.ok ? "✓" : c.informational ? "!" : "✗"}</span>
      <div><strong>${esc(c.name)}</strong><div class="small muted">${esc(c.detail)}</div></div></div>`).join("") : '<div class="muted">No balance or total found to check against.</div>'}
    <div class="small" style="margin-top:6px">Opening ${money(p.opening)} · Closing ${money(p.closing)}${p.closing_printed !== null ? ` (printed ${money(p.closing_printed)})` : ""}</div>
    ${(p.ai_log || []).length ? `<div class="info small">${p.ai_log.map(esc).join("<br>")}</div>` : ""}
    ${tables}
    <div class="row" style="margin-top:10px"><button class="secondary applymap" data-side="${side}" disabled>Re-read with these columns</button></div>
    <details style="margin-top:10px"><summary>Notes (${p.notes.length}) and flagged rows (${p.flagged.length})</summary>
      <ul class="small">${p.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>
      ${p.flagged.length ? `<table><tbody>${p.flagged.slice(0, 40).map(e => `<tr><td>row ${e.rows[0]}</td><td>${esc(e.ref)}</td><td class="small">${esc(e.flags.join("; "))}</td></tr>`).join("")}</tbody></table>` : ""}
    </details>
    <details><summary>All extracted rows (${p.entries})</summary><div class="scroll">
      <table><thead><tr><th>Date</th><th>Ref</th><th>Type</th><th>Invoice</th><th class="num">Debit</th><th class="num">Credit</th><th class="num">TDS</th><th>Narration</th></tr></thead><tbody>
      ${(p.all_entries || p.sample_entries).map(e => `<tr><td>${dmy(e.date)}</td><td>${esc(e.ref)}</td><td>${esc(e.kind)}</td><td>${esc(e.key)}</td><td class="num">${e.debit ? money(e.debit) : ""}</td>
        <td class="num">${e.credit ? money(e.credit) : ""}</td><td class="num">${e.tds ? money(e.tds) : ""}</td><td class="small">${esc(e.narration)}</td></tr>`).join("")}</tbody></table></div>
    </details>
  </div>`;
}

// A proven ledger needs no column editing: show only that it is proven and its key figures.
function provenPanel(p, title, filename) {
  return `<div class="panel">
    <div class="row"><h2 style="margin:0">${esc(title)}</h2><div class="spacer"></div>${badge(p.confidence)}</div>
    <div class="muted small">${esc(filename)}</div>
    <table style="margin-top:10px"><tbody>
      <tr><td>Period</td><td class="num">${dmy(p.first_date)} to ${dmy(p.last_date)}</td></tr>
      <tr><td>Entries</td><td class="num">${p.entries}</td></tr>
      <tr><td>Opening balance</td><td class="num">${money(p.opening)}</td></tr>
      <tr class="total"><td>Closing balance</td><td class="num">${money(p.closing)}</td></tr>
    </tbody></table>
  </div>`;
}

// ---------- step 2: results
function resultsView(run) {
  const R = run.result, B = R.bridge, cp = esc(run.counterparty);
  const ok = Math.abs(B.residual) < 0.005;
  // Nothing unexplained: one message and the report, no breakdown.
  if (ok) return `
  <div class="tile ok" style="padding:28px;text-align:center">
    <div style="font-size:40px;line-height:1">✓</div>
    <div style="font-size:22px;font-weight:700;margin-top:8px">Reconciled</div>
    <div class="small" style="margin-top:6px">${run.preview && run.preview.W && run.preview.W.confidence === "green" && run.preview.C && run.preview.C.confidence === "green"
      ? "Both ledgers are proven, and every" : "Every"} difference between the two ledgers is accounted for. Unexplained difference: ${money(0)}</div>
    ${openItems(B)}
    <div style="margin-top:18px"><a class="btn" href="/api/runs/${esc(run.id)}/report.xlsx">Download Excel report</a></div>
  </div>`;
  const tiers = Object.entries(R.tiers).sort();
  const tierOpts = tiers.map(([t]) => `<option ${VIEW.tier === t ? "selected" : ""}>${esc(t)}</option>`).join("");
  const buckets = [...new Set(R.unmatched.map(u => u.bucket))];
  return `
  <div class="row" style="margin-bottom:12px"><button class="secondary" id="tocheck">Check columns again</button><div class="spacer"></div>
    <a class="btn" href="/api/runs/${esc(run.id)}/report.xlsx">Download Excel report</a></div>
  <div class="tiles">
    <div class="tile"><div class="k">Walkover closing (from its ledger)</div><div class="v">${money(B.w_closing)}</div></div>
    <div class="tile"><div class="k">${cp} closing (in Walkover's direction)</div><div class="v">${money(B.c_closing)}</div></div>
    <div class="tile ${ok ? "ok" : "bad"}"><div class="k">Unexplained difference</div><div class="v">${money(B.residual)}</div></div>
  </div>
  <div class="panel"><h2>Bridge</h2>
    <p class="muted small">Every difference sits in a named line. + means the counterparty owes Walkover more.</p>
    <table><thead><tr><th>Line</th><th class="num">Items</th><th class="num">Amount</th></tr></thead><tbody>
      <tr><td><strong>Walkover closing balance</strong></td><td></td><td class="num">${money(B.w_closing)}</td></tr>
      ${B.lines.map(l => `<tr><td>${esc(l.label)} <span class="chip">${esc(l.category)}</span></td><td class="num">${l.count}</td><td class="num">${money(l.amount)}</td></tr>`).join("")}
      <tr class="total"><td>Derived ${cp} closing</td><td></td><td class="num">${money(B.derived)}</td></tr>
      <tr><td>${cp} closing per its ledger</td><td></td><td class="num">${money(B.c_closing)}</td></tr>
      <tr class="total"><td>Unexplained</td><td></td><td class="num">${money(B.residual)}</td></tr>
    </tbody></table>
    ${R.notes.length ? `<p class="small muted">${R.notes.map(esc).join(" ")}</p>` : ""}
  </div>
  ${tdsPanel(R.tds, cp)}
  <div class="panel">
    <div class="tabs">
      <div class="tab ${VIEW.tab === "matches" ? "active" : ""}" data-tab="matches">Matched (${R.matches.length})</div>
      <div class="tab ${VIEW.tab === "unmatched" ? "active" : ""}" data-tab="unmatched">Unmatched (${R.unmatched.length})</div>
      <div class="tab ${VIEW.tab === "chains" ? "active" : ""}" data-tab="chains">Chains & self-cancelling (${R.chains.length + R.self_cancel.length})</div>
    </div>
    <div class="row" style="margin-bottom:10px">
      ${VIEW.tab === "matches" ? `<select id="ftier" style="max-width:260px"><option value="">All tiers</option>${tierOpts}</select>` : ""}
      ${VIEW.tab === "unmatched" ? `<select id="fbucket" style="max-width:420px"><option value="">All buckets</option>${buckets.map(b => `<option ${VIEW.bucket === b ? "selected" : ""}>${esc(b)}</option>`).join("")}</select>` : ""}
      ${VIEW.tab !== "chains" ? `<input type="text" id="fq" placeholder="Search ref, key or narration" value="${esc(VIEW.q)}" style="max-width:300px">` : ""}
      <span class="muted small">${tiers.map(([t, n]) => `${esc(t)}: ${n}`).join(" · ")}</span>
    </div>
    <div id="tabbody">${tabBody(R, cp)}</div>
  </div>`;
}

// Entries one side has not yet accounted: reconciled (explained), but still open, so never hidden.
function openItems(B) {
  const open = B.lines.filter(l => l.category === "not accounted" || l.category === "timing");
  if (!open.length) return "";
  return `<div class="info small" style="margin-top:14px;text-align:left"><strong>Open reconciling items</strong> (explained, still to be accounted):
    <ul style="margin:6px 0 0 18px;padding:0">${open.map(l => `<li>${esc(l.label)}: ${l.count} item${l.count === 1 ? "" : "s"}, ${money(l.amount)}</li>`).join("")}</ul></div>`;
}

function aiPanel(A) {
  if (!A) return "";
  if (!A.enabled) return `<div class="panel small muted">AI fallback is off on this server (no API key set). Everything above was settled by the rules alone.</div>`;
  if (!A.calls) return `<div class="panel small muted">AI fallback was not needed: the rules settled everything they could.</div>`;
  return `<div class="panel"><h2>AI assistance</h2>
    <p class="small muted">AI was asked only about what the rules left open. Each suggestion was re-checked by the maths: amounts must tie and every line is used once.</p>
    <div class="row small"><span class="badge grey">${A.calls} call(s)</span><span class="badge green">${A.accepted} accepted</span>
      <span class="badge ${A.rejected ? "amber" : "grey"}">${A.rejected} rejected by the maths</span>${A.failures ? `<span class="badge red">${A.failures} failed</span>` : ""}</div>
    ${A.log.length ? `<details style="margin-top:8px"><summary>What AI was asked and what happened</summary><ul class="small">${A.log.map(l => `<li>${esc(l)}</li>`).join("")}</ul></details>` : ""}
  </div>`;
}

function tdsPanel(T, cp) {
  if (!T || !T.years.length) return "";
  return `<div class="panel"><h2>TDS by financial year</h2>
    <p class="muted small">Rate found in the data: ${esc(T.rate)}. A deduction is placed in the year of its provision month only where that makes the years agree.</p>
    <table><thead><tr><th>Year</th><th class="num">Deducted by counterparty</th><th class="num">Booked by Walkover</th><th class="num">Difference</th><th>Moved into this year</th></tr></thead><tbody>
    ${T.years.map(y => `<tr><td>${esc(y.fy)}</td><td class="num">${money(y.counterparty)}</td><td class="num">${money(y.walkover)}</td><td class="num">${money(y.diff)}</td><td class="small">${esc(y.moved)}</td></tr>`).join("")}
    </tbody></table>
    ${T.excess.length ? `<h3>Withheld beyond TDS</h3><ul>${T.excess.map(x => `<li>${esc(x.key)}: ${money(x.amount)} <span class="small muted">${esc(x.source)}</span></li>`).join("")}</ul>` : ""}
    ${T.other.length ? `<h3>Other deductions (not TDS)</h3><ul>${T.other.map(x => `<li>${esc(x.key)}: ${money(x.amount)} <span class="small muted">${esc(x.narration)}</span></li>`).join("")}</ul>` : ""}
  </div>`;
}

function tabBody(R, cp) {
  const q = VIEW.q.toLowerCase();
  const hit = s => !q || String(s).toLowerCase().includes(q);
  if (VIEW.tab === "matches") {
    const rows = R.matches.filter(m => (!VIEW.tier || m.tier === VIEW.tier) && hit([m.key, ...m.w_refs, ...m.c_refs, m.note].join(" ")));
    return `<div class="scroll"><table><thead><tr><th>Tier</th><th>Key</th><th>Walkover</th><th class="num">Walkover amount</th><th>Counterparty</th><th class="num">Counterparty amount</th><th class="num">Difference</th><th>Explained as</th></tr></thead><tbody>
      ${rows.map(m => `<tr><td>${esc(m.tier)}</td><td>${esc(m.key)}</td>
        <td class="small">${esc(m.w_refs.join(", "))}<br><span class="muted">${dmy(m.w_date)}</span></td><td class="num">${money(m.w_amount)}</td>
        <td class="small">${esc(m.c_refs.join(", "))}<br><span class="muted">${dmy(m.c_date)}</span></td><td class="num">${money(m.c_amount)}</td>
        <td class="num">${Math.abs(m.diff) < 0.005 ? "" : money(m.diff)}</td>
        <td class="small">${m.parts.map(p => `${esc(p.label)}: ${money(p.amount)}`).join("<br>") || '<span class="muted">exact</span>'}${m.note ? `<br><span class="muted">${esc(m.note)}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div><div class="small muted" style="margin-top:6px">${rows.length} shown</div>`;
  }
  if (VIEW.tab === "unmatched") {
    const rows = R.unmatched.filter(u => (!VIEW.bucket || u.bucket === VIEW.bucket) && hit([u.ref, u.key, u.narration].join(" ")));
    return `<div class="scroll"><table><thead><tr><th>Ledger</th><th>Date</th><th>Ref</th><th>Type</th><th class="num">Amount</th><th>Bucket</th><th>Narration</th><th>Notes</th></tr></thead><tbody>
      ${rows.map(u => `<tr><td>${u.side === "W" ? "Walkover" : "Counterparty"}</td><td>${dmy(u.date)}</td><td>${esc(u.ref)}</td><td>${esc(u.kind)}</td>
        <td class="num">${money(u.amount)}</td><td class="small">${esc(u.bucket)}</td><td class="small">${esc(u.narration)}</td>
        <td class="small">${esc((u.flags || []).filter(f => f.startsWith("AI") || f.startsWith("type set by AI")).join("; "))}</td></tr>`).join("")}
      </tbody></table></div><div class="small muted" style="margin-top:6px">${rows.length} shown</div>`;
  }
  return `${R.chains.length ? `<h3>Revision chains</h3><table><thead><tr><th>Ledger</th><th>Credit note</th><th>Cancels</th><th>Re-billed as</th><th>Note</th></tr></thead><tbody>
      ${R.chains.map(c => `<tr><td>${c.side === "W" ? "Walkover" : "Counterparty"}</td><td>${esc(c.credit_note)}</td><td>${esc(c.cancels.join(", "))}</td><td>${esc(c.rebill.join(", "))}</td>
        <td class="small">${c.net_nil ? "credit note equals the invoices it cancels" : "credit note and cancelled invoices differ in these books"}</td></tr>`).join("")}</tbody></table>` : ""}
    ${R.self_cancel.length ? `<h3>Self-cancelling sets (net nil inside one ledger)</h3>${R.self_cancel.map(g => `<table style="margin-bottom:10px"><tbody>
      ${g.map(e => `<tr><td>${dmy(e.date)}</td><td>${esc(e.ref)}</td><td>${esc(e.kind)}</td><td class="num">${money(e.debit)}</td><td class="num">${money(e.credit)}</td><td class="small">${esc(e.narration)}</td></tr>`).join("")}
      </tbody></table>`).join("")}` : ""}
    ${!R.chains.length && !R.self_cancel.length ? '<p class="muted">None found.</p>' : ""}`;
}

// ---------- events
function bindRun(run) {
  const id = run.id;
  const rec = document.getElementById("reconcile");
  if (rec) rec.onclick = async () => {
    rec.disabled = true;
    try { await api(`/api/runs/${id}/reconcile`, { method: "POST" }); VIEW.showCheck = false; renderRun(id); }
    catch (e) { if (e.message !== "signin") alert(e.message); rec.disabled = false; }
  };
  const toCheck = document.getElementById("tocheck");
  if (toCheck) toCheck.onclick = () => { VIEW.showCheck = true; renderRun(id); };
  const toRes = document.getElementById("toresults");
  if (toRes) toRes.onclick = () => { VIEW.showCheck = false; renderRun(id); };

  document.querySelectorAll("select.sheet").forEach(sel => sel.onchange = async () => {
    await api(`/api/runs/${id}/mapping`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ side: sel.dataset.side, sheet: sel.value }) });
    renderRun(id);
  });
  document.querySelectorAll("select.role").forEach(sel => sel.onchange = () => {
    sel.classList.toggle("changed", sel.value !== sel.dataset.orig);
    const side = sel.dataset.side;
    const changed = [...document.querySelectorAll(`select.role[data-side="${side}"]`)].some(s => s.value !== s.dataset.orig);
    document.querySelector(`.applymap[data-side="${side}"]`).disabled = !changed;
  });
  document.querySelectorAll(".applymap").forEach(btn => btn.onclick = async () => {
    const side = btn.dataset.side, roles = {};
    const seen = {};
    for (const s of document.querySelectorAll(`select.role[data-side="${side}"]`)) {
      if (!s.value) continue;
      const t = s.dataset.table;
      roles[t] = roles[t] || {};
      if (roles[t][s.value] !== undefined) { alert(`"${s.options[s.selectedIndex].text}" is chosen for two columns.`); return; }
      roles[t][s.value] = +s.dataset.col;
    }
    btn.disabled = true;
    await api(`/api/runs/${id}/mapping`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ side, roles }) });
    renderRun(id);
  });
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => { VIEW.tab = t.dataset.tab; VIEW.q = ""; renderRun(id); });
  const R = run.result, cp = esc(run.counterparty);
  const redraw = () => { document.getElementById("tabbody").innerHTML = tabBody(R, cp); };
  const ft = document.getElementById("ftier"); if (ft) ft.onchange = () => { VIEW.tier = ft.value; redraw(); };
  const fb = document.getElementById("fbucket"); if (fb) fb.onchange = () => { VIEW.bucket = fb.value; redraw(); };
  const fq = document.getElementById("fq"); if (fq) fq.oninput = () => { VIEW.q = fq.value; redraw(); };
}

// ------------------------------------------------------------------ payment check (one Walkover ledger)
async function renderPayHome() {
  $app.innerHTML = `${featureTabs("pay")}
  <p class="muted">Upload Walkover's ledger with one row per invoice and the payment received against it. Each payment is checked against the invoice amount less 2% TDS, then 10% TDS, and the Remaining column is filled in.</p>
  <div class="panel">
    <h2>New payment check</h2>
    <form id="payform">
      <label for="pname">Name <span class="muted small">(optional, e.g. the customer)</span></label>
      <input type="text" id="pname" name="name" placeholder="e.g. Acme payments FY26-27">
      <label for="pf">Walkover's ledger</label><input type="file" id="pf" name="file" accept=".xlsx,.xls,.xlsm,.csv">
      <div class="small muted" style="margin:6px 0 2px">or paste a Google Sheets link</div>
      <input type="text" name="url" placeholder="https://docs.google.com/spreadsheets/d/…">
      <p class="small muted">Excel, CSV or a Google Sheet shared as "Anyone with the link can view". It needs an invoice amount column and a payment column; TDS, amount after TDS, dates and Remaining are used when present.</p>
      <div class="row" style="margin-top:14px"><button type="submit" id="pgo">Check payments</button><span id="pmsg" class="muted"></span></div>
    </form>
  </div>
  <div class="panel"><h2>Recent</h2><div id="precent" class="muted">Loading…</div></div>`;
  document.getElementById("payform").addEventListener("submit", async ev => {
    ev.preventDefault();
    const btn = document.getElementById("pgo"), msg = document.getElementById("pmsg");
    const fd = new FormData(ev.target);
    const f = fd.get("file"); if (f && !f.name) fd.delete("file");
    btn.disabled = true; msg.innerHTML = '<span class="spinner"></span>Uploading…';
    try {
      const r = await api("/api/checks", { method: "POST", body: fd });
      location.hash = `#/check/${r.id}`;
    } catch (e) { if (e.message !== "signin") { msg.innerHTML = `<span class="neg">${esc(e.message)}</span>`; btn.disabled = false; } }
  });
  try {
    const list = await api("/api/checks");
    const el = document.getElementById("precent");
    if (!list.length) { el.textContent = "No payment checks yet."; return; }
    el.classList.remove("muted");
    el.innerHTML = `<table><thead><tr><th>Name</th><th>File</th><th>Started</th><th>By</th><th>Result</th><th class="num">Remaining</th></tr></thead><tbody>
      ${list.map(c => `<tr><td><a href="#/check/${esc(c.id)}">${esc(c.name)}</a></td><td class="small">${esc(c.filename)}</td>
        <td>${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</td><td class="small">${esc(c.created_by)}</td>
        <td>${checkBadge(c)}</td><td class="num">${c.summary ? money(c.summary.remaining_total / 100) : ""}</td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) { /* handled */ }
}

function checkBadge(c) {
  if (c.status === "checking") return '<span class="badge grey">Checking</span>';
  if (c.status === "failed") return '<span class="badge red">Failed</span>';
  if (c.summary && c.summary.all_clear) return '<span class="badge green">All reconciled</span>';
  return `<span class="badge amber">${c.summary ? c.summary.open : "?"} remaining</span>`;
}

const PAY_STATUS = { matched: "Matched", paid_full: "Paid in full", unpaid: "Not paid", short: "Short paid", excess: "Excess paid" };

async function renderCheck(id) {
  let c;
  try { c = await api(`/api/checks/${id}`); } catch (e) { if (e.message !== "signin") $app.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  let html = `<div class="row"><div><a href="#/payments">← All payment checks</a><h1>${esc(c.name)}</h1>
      <div class="muted small">${esc(c.filename)} · started ${esc((c.created_at || "").slice(0, 16).replace("T", " "))} by ${esc(c.created_by)}</div></div>
      <div class="spacer"></div>${checkBadge(c)}</div>`;
  if (c.status === "checking") {
    $app.innerHTML = html + `<div class="panel" style="margin-top:16px"><span class="spinner"></span>Reading the columns and checking every payment…</div>`;
    pollTimer = setTimeout(() => renderCheck(id), 1200);
    return;
  }
  if (c.error) html += `<div class="error">${esc(c.error)}</div>`;
  const R = c.result;
  if (!R) { $app.innerHTML = html; return; }
  const S = R.summary, dl = `<a class="btn" href="/api/checks/${esc(c.id)}/output.xlsx">Download Excel</a>`;
  html += `<div style="margin-top:16px"></div>`;
  if (R.confidence === "green" && S.all_clear) {
    html += `<div class="tile ok" style="padding:28px;text-align:center">
      <div style="font-size:40px;line-height:1">✓</div>
      <div style="font-size:22px;font-weight:700;margin-top:8px">All payments reconciled</div>
      <div class="small" style="margin-top:6px">${S.invoices} invoices · every payment matches its invoice less TDS. Remaining: ${money(0)}</div>
      ${noTdsNote(R)}
      <div style="margin-top:18px">${dl}</div></div>`;
    $app.innerHTML = html; bindCheck(c); return;
  }
  const open = R.rows.filter(x => x.remaining !== 0);
  html += `<div class="tiles">
      <div class="tile"><div class="k">Invoices</div><div class="v">${S.invoices}</div></div>
      <div class="tile"><div class="k">Matched (2%: ${S.matched_2} · 10%: ${S.matched_10}${S.paid_full ? ` · no TDS: ${S.paid_full}` : ""})</div><div class="v">${S.invoices - S.open}</div></div>
      <div class="tile ${S.open ? "bad" : "ok"}"><div class="k">Remaining (${S.open} invoice${S.open === 1 ? "" : "s"})</div><div class="v">${money(S.remaining_total / 100)}</div></div>
    </div>`;
  if (R.confidence !== "green") html += checkColumns(c);
  if (S.paid_full) html += `<div class="panel">${noTdsNote(R)}</div>`;
  if (open.length) html += `<div class="panel"><div class="row"><h2 style="margin:0">Invoices with an amount remaining</h2><div class="spacer"></div>${dl}</div>
    <div class="scroll" style="margin-top:12px"><table><thead><tr><th>Row</th><th>Invoice</th><th>Date</th><th class="num">Invoice amount</th><th class="num">Expected (less TDS)</th><th class="num">Payment</th><th>Paid on</th><th class="num">Remaining</th><th>Result</th></tr></thead><tbody>
    ${open.map(x => `<tr><td>${x.row}</td><td>${esc(x.invoice_no)}</td><td>${dmy(x.invoice_date)}</td><td class="num">${money(x.invoice / 100)}</td>
      <td class="num">${money(x.expected / 100)} <span class="muted small">(${esc(x.rate)})</span></td><td class="num">${x.payment ? money(x.payment / 100) : ""}</td>
      <td>${dmy(x.received_date)}</td><td class="num"><strong>${money(x.remaining / 100)}</strong></td>
      <td class="small"><span class="badge ${x.status === "unpaid" ? "grey" : "amber"}">${esc(PAY_STATUS[x.status])}</span> ${esc(x.note)}</td></tr>`).join("")}
    </tbody></table></div></div>`;
  else html += `<div class="panel row">${dl}</div>`;
  $app.innerHTML = html;
  bindCheck(c);
}

function noTdsNote(R) {
  const rows = R.rows.filter(x => x.status === "paid_full");
  if (!rows.length) return "";
  return `<div class="info small" style="margin-top:12px;text-align:left"><strong>Customer paid the full amount, no TDS deducted</strong> (counted as matched):
    ${rows.map(x => `row ${x.row}${x.invoice_no ? ` (invoice ${esc(x.invoice_no)}, ${money(x.invoice / 100)})` : ""}`).join(", ")}</div>`;
}

function checkColumns(c) {
  const R = c.result, used = R.cols;
  const opts = sel => `<option value="">— none —</option>` + R.columns.map(col =>
    `<option value="${col.col}" ${sel === col.col ? "selected" : ""}>${esc(col.letter)} · ${esc(col.label || "(no header)")}</option>`).join("");
  return `<div class="panel"><div class="row"><h2 style="margin:0">How the columns were read</h2><div class="spacer"></div>${badge(R.confidence)}</div>
    <p class="small muted">${R.confidence === "red" ? "The columns could not be read with certainty." : "The reading could not be fully proven against the sheet's own numbers."} Check the columns below; change any that are wrong and re-check.${R.mapped_by === "ai" ? " (Columns mapped by AI.)" : ""}</p>
    ${R.checks.map(k => `<div class="check ${k.ok ? "ok" : k.informational ? "note" : "no"}"><span class="mark">${k.ok ? "✓" : k.informational ? "!" : "✗"}</span><div><strong>${esc(k.name)}</strong><div class="small muted">${esc(k.detail)}</div></div></div>`).join("")}
    ${R.sheets.length > 1 ? `<label>Sheet</label><select id="csheet">${R.sheets.map(s => `<option ${s === R.sheet ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>` : ""}
    <label>Header row</label><input type="text" id="chead" value="${R.header_row || 1}" style="max-width:100px">
    <div class="grid2" style="margin-top:6px">${c.role_choices.map(r => `<div><label>${esc(r.label)}</label><select class="crole" data-role="${r.value}">${opts(used[r.value])}</select></div>`).join("")}</div>
    <div class="row" style="margin-top:12px"><button class="secondary" id="crerun">Re-check with these columns</button></div>
    ${R.notes.length || (R.ai && R.ai.log.length) ? `<details style="margin-top:10px"><summary>Notes</summary><ul class="small">${[...R.notes, ...(R.ai ? R.ai.log : [])].map(n => `<li>${esc(n)}</li>`).join("")}</ul></details>` : ""}
  </div>`;
}

function bindCheck(c) {
  const post = async body => {
    try { await api(`/api/checks/${c.id}/mapping`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); renderCheck(c.id); }
    catch (e) { if (e.message !== "signin") alert(e.message); }
  };
  const sh = document.getElementById("csheet");
  if (sh) sh.onchange = () => post({ sheet: sh.value });
  const re = document.getElementById("crerun");
  if (re) re.onclick = () => {
    const cols = {};
    for (const s of document.querySelectorAll("select.crole")) if (s.value !== "") cols[s.dataset.role] = +s.value;
    post({ header_row: +document.getElementById("chead").value || 1, cols });
  };
}

route();
