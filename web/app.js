// Dashboard + browser call client. No build step: the Vapi web SDK is loaded as an ES module from a CDN.

const $ = (s) => document.querySelector(s);
const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN");
const STATUS_LABEL = {
  pending: "pending", failed: "retry failed", recovered: "recovered", promise_to_pay: "promise to pay",
  link_sent: "link sent", needs_new_mandate: "new mandate link sent", escalated: "escalated",
  do_not_call: "do not call", wrong_number: "wrong number",
};
const REASON_LABEL = {
  insufficient_funds: "Insufficient funds", bank_server_down: "Bank server down", technical_decline: "Technical decline",
  mandate_paused: "Mandate paused", card_expired: "Card expired", mandate_limit_exceeded: "Mandate limit exceeded",
  mandate_revoked: "Mandate revoked", account_frozen: "Account on hold",
};

let cfg = {};
let vapi = null;
let active = { callId: null, customerId: null, poll: null, seenEvents: 0 };

// ---------------------------------------------------------------- data

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function refresh() {
  const [customers, calls] = await Promise.all([api("/api/customers"), api("/api/calls")]);
  renderMetrics(customers, calls);
  renderCustomers(customers);
  renderHistory(calls, customers);
}

// ---------------------------------------------------------------- render

function renderChips() {
  $("#merchant").textContent = cfg.merchant_name;
  $("#chips").innerHTML = [
    ["Web calls", cfg.web_calls_ready], ["Phone calls", cfg.phone_calls_ready],
  ].map(([k, on]) => `<span class="chip ${on ? "on" : "off"}">${k}: ${on ? "ready" : "not configured"}</span>`).join("")
    + `<span class="chip">Calling window ${cfg.calling_window}</span><span class="chip">Max ${cfg.max_attempts} attempts</span>`;
}

function renderMetrics(customers, calls) {
  const st = customers.map((c) => c.state);
  const recovered = st.filter((s) => s.recovery_status === "recovered");
  const total = customers.reduce((a, c) => a + c.amount_inr, 0);
  const tiles = [
    ["Recovered", inr(recovered.reduce((a, s) => a + (s.recovered_amount_inr || 0), 0)) + ` <span class="dim">of ${inr(total)}</span>`],
    ["Recovered accounts", `${recovered.length} / ${customers.length}`],
    ["Promise to pay", st.filter((s) => s.recovery_status === "promise_to_pay").length],
    ["Links sent", st.filter((s) => ["link_sent", "needs_new_mandate"].includes(s.recovery_status)).length],
    ["Escalated", st.filter((s) => s.recovery_status === "escalated").length],
    ["Opted out / wrong no.", st.filter((s) => ["do_not_call", "wrong_number"].includes(s.recovery_status)).length],
    ["Calls made", calls.length],
  ];
  $("#metrics").innerHTML = tiles.map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}

function renderCustomers(customers) {
  const tb = $("#customers tbody");
  tb.innerHTML = customers.map((c) => {
    const s = c.state;
    const linkBtn = s.payment_link && !s.link_paid
      ? `<button class="small ghost" data-act="linkpaid" data-id="${c.id}" title="Stand-in for the gateway's payment-link webhook">Simulate link paid</button>` : "";
    const phoneBtn = cfg.phone_calls_ready
      ? `<button class="small ghost" data-act="phone" data-id="${c.id}">Call my phone</button>` : "";
    const bank = `bank: mandate ${String(s.mandate_status || "active").replace(/_/g, " ")} · ${s.funds_available ? "funds ok" : "low balance"}`;
    const sub = [bank, s.scheduled_retry ? `retry ${s.scheduled_retry}` : "", s.callback_time ? `callback ${s.callback_time}` : "",
      s.escalation ? `“${s.escalation}”` : "", s.payment_link ? s.payment_link.url : ""].filter(Boolean).join(" · ");
    return `<tr>
      <td><div class="name">${c.name}</div><div class="dim">${c.id} · ${c.preferred_language}</div></td>
      <td>${c.plan}<div class="dim">${c.mandate_type}</div></td>
      <td>${inr(c.amount_inr)}</td>
      <td>${REASON_LABEL[c.failure_reason] || c.failure_reason}<div class="dim">failed ${c.failed_on}</div></td>
      <td>${s.attempts}</td>
      <td><span class="badge ${s.recovery_status}">${STATUS_LABEL[s.recovery_status] || s.recovery_status}</span>
          <div class="dim">${c.eligible ? "eligible" : c.eligibility_reason}</div>${sub ? `<div class="dim">${sub}</div>` : ""}</td>
      <td><div class="row-actions">
        <button class="small" data-act="web" data-id="${c.id}" ${cfg.web_calls_ready && !active.callId && !s.do_not_call ? "" : "disabled"}
          title="${s.do_not_call ? "Opted out: do-not-call is never bypassed, even in demo mode" : ""}">Call in browser</button>
        ${phoneBtn}${linkBtn}
      </div></td>
    </tr>`;
  }).join("");
}

function renderHistory(calls, customers) {
  const byId = Object.fromEntries(customers.map((c) => [c.id, c]));
  if (!calls.length) { $("#history").innerHTML = `<div class="hint">No calls yet. Run a browser call, or <code>python -m scripts.dry_run</code> to replay ten simulated calls through the webhook.</div>`; return; }
  $("#history").innerHTML = calls.map((k) => {
    const c = byId[k.customer_id];
    const sd = k.structured || {};
    const outcome = k.outcome || sd.outcome || k.status || "";
    return `<details class="hrow"><summary>
        <b>${c ? c.name : k.customer_id || "unknown"}</b>
        <span class="badge ${outcome}">${outcome.replace(/_/g, " ")}</span>
        <span class="dim">${k.channel} · ${k.started_at ? new Date(k.started_at).toLocaleString() : ""} · ${k.duration_seconds ? Math.round(k.duration_seconds) + "s" : ""} · ${k.success_eval ? "eval " + k.success_eval : ""}</span>
      </summary>
      ${k.summary ? `<p>${k.summary}</p>` : ""}
      ${sd.follow_up_note ? `<p class="dim">Follow-up: ${sd.follow_up_note}</p>` : ""}
      <button class="small ghost" data-act="review" data-id="${k.call_id}">Review in call panel</button>
      ${k.transcript ? `<pre>${escapeHtml(k.transcript)}</pre>` : ""}
    </details>`;
  }).join("");
}

function escapeHtml(s) { return String(s).replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch])); }

function addLine(role, text, extra = "") {
  const el = document.createElement("div");
  el.className = `line ${role} ${extra}`;
  const who = role === "assistant" ? cfg.agent_name : role === "user" ? "Customer" : extra === "act" ? "outside the call" : "tool";
  el.innerHTML = `<span class="who">${who}</span>${escapeHtml(text)}`;
  $("#transcript").appendChild(el);
  $("#transcript").scrollTop = $("#transcript").scrollHeight;
}

// ---------------------------------------------------------------- calls

async function loadVapi() {
  if (vapi) return vapi;
  const sources = ["https://esm.sh/@vapi-ai/web", "https://cdn.jsdelivr.net/npm/@vapi-ai/web/+esm"];
  let mod = null, lastErr = null;
  for (const src of sources) { try { mod = await import(src); break; } catch (e) { lastErr = e; } }
  if (!mod) throw new Error("Could not load the Vapi web SDK from a CDN: " + lastErr);
  const Vapi = mod.default || mod.Vapi;
  vapi = new Vapi(cfg.vapi_public_key);
  vapi.on("call-start", () => { $("#call-meta").textContent = "Connected. Speak as the customer."; $("#hangup-btn").disabled = false; $("#mute-btn").disabled = false; });
  vapi.on("call-end", () => endCallUi("Call ended. Waiting for the end-of-call report…"));
  vapi.on("error", (e) => { console.error(e); endCallUi("Call error: " + (e?.message || JSON.stringify(e))); });
  vapi.on("message", (m) => {
    if (m.type === "transcript" && m.transcriptType === "final") addLine(m.role, m.transcript);
  });
  return vapi;
}

async function renderPersona(customerId) {
  const secret = await api(`/api/customers/${customerId}/secret`);
  const actions = secret.actions.length
    ? `<div class="cust-actions"><b>Things the real customer would do on their own phone</b> (the agent cannot see these; it has to check with the bank):<br>`
      + secret.actions.map((a) => `<button class="small" data-cact="${a.action}" data-id="${customerId}">${a.label}</button>`).join(" ") + `</div>`
    : `<div class="cust-actions dim">No pending customer-side action: the bank-side state already allows the next step.</div>`;
  $("#persona").hidden = false;
  $("#persona").innerHTML = `<b>You are playing:</b> ${secret.persona}<br>When asked, the 4 digits are <code>${secret.verify_last4}</code>. You can also go off-script (ask to stop calls, claim you cancelled, say it's a wrong number).${actions}`;
}

async function startWebCall(customerId) {
  try {
    const { assistant_id, assistant_overrides } = await api(`/api/web-call/${customerId}?force=true`);
    await renderPersona(customerId);
    $("#transcript").innerHTML = ""; $("#outcome").hidden = true; $("#transcript").classList.remove("full");
    $("#call-meta").textContent = "Connecting… allow microphone access.";
    const v = await loadVapi();
    const call = await v.start(assistant_id, assistant_overrides);
    active = { callId: call?.id || null, customerId, poll: null, seenEvents: 0 };
    if (active.callId) {
      await api("/api/calls/register", { method: "POST", body: JSON.stringify({ call_id: active.callId, customer_id: customerId, channel: "web" }) });
      active.poll = setInterval(pollCall, 2000);
    }
    refresh();
  } catch (e) { endCallUi("Could not start call: " + e.message); }
}

function renderEvent(ev, withTranscript) {
  if (ev.kind === "tool") addLine("tool", `${ev.name}(${JSON.stringify(ev.args)}) → ${ev.error || ev.result}`, ev.error ? "err" : "");
  else if (ev.kind === "action") addLine("tool", ev.text, "act");
  else if (withTranscript && ev.kind === "transcript") addLine(ev.role, ev.text);
}

// Re-open any past call in the call panel: full transcript, every tool call, and the outcome.
async function reviewCall(callId, scroll = true) {
  const k = await api(`/api/calls/${callId}`);
  $("#transcript").innerHTML = ""; $("#persona").hidden = true;
  $("#transcript").classList.add("full");          // reviewing: show the whole call, no inner scroll
  (k.events || []).forEach((ev) => renderEvent(ev, true));
  showOutcome(k);
  $("#call-meta").textContent = `Reviewing ${k.channel} call ${k.call_id} · ${k.started_at ? new Date(k.started_at).toLocaleString() : ""}`;
  if (scroll) $("#call-card").scrollIntoView({ block: "start" });
}

async function pollCall() {
  if (!active.callId) return;
  try {
    const k = await api(`/api/calls/${active.callId}`);
    const events = k.events || [];
    events.slice(active.seenEvents).forEach((ev) => renderEvent(ev, false));
    active.seenEvents = events.length;
    if (k.outcome || k.summary || k.structured) {
      showOutcome(k);
      clearInterval(active.poll); active = { callId: null, customerId: null, poll: null, seenEvents: 0 };
      refresh();
    }
  } catch (e) { /* call row may not exist yet */ }
}

function showOutcome(k) {
  const sd = k.structured || {};
  $("#outcome").hidden = false;
  $("#outcome").innerHTML = `<b>Post-call analysis</b><dl>
    <dt>Outcome</dt><dd><span class="badge ${k.outcome || ""}">${(k.outcome || "—").replace(/_/g, " ")}</span> <span class="dim">from tool results</span></dd>
    <dt>Summary</dt><dd>${k.summary || "—"}</dd>
    <dt>Sentiment</dt><dd>${sd.customer_sentiment || "—"}</dd>
    <dt>Verified</dt><dd>${sd.identity_verified ?? "—"}</dd>
    <dt>Promise date</dt><dd>${sd.promise_to_pay_date || "—"}</dd>
    <dt>Follow-up</dt><dd>${sd.follow_up_required ? (sd.follow_up_note || "yes") : "none"}</dd>
    <dt>Guardrail eval</dt><dd>${k.success_eval || "—"}</dd>
    <dt>Duration / cost</dt><dd>${k.duration_seconds ? Math.round(k.duration_seconds) + "s" : "—"} / ${k.cost_usd != null ? "$" + Number(k.cost_usd).toFixed(3) : "—"}</dd>
  </dl>`;
  $("#call-meta").textContent = "Call complete.";
}

function endCallUi(msg) {
  $("#call-meta").textContent = msg;
  $("#hangup-btn").disabled = true; $("#mute-btn").disabled = true; $("#mute-btn").textContent = "Mute";
  // keep polling a little longer for the end-of-call report
  if (active.callId && !active.poll) active.poll = setInterval(pollCall, 2000);
  setTimeout(() => { if (active.poll) { clearInterval(active.poll); active = { callId: null, customerId: null, poll: null, seenEvents: 0 }; refresh(); } }, 90000);
  refresh();
}

async function startPhoneCall(customerId) {
  const to = prompt("Dial which number? Use only a number you own or have permission to call (E.164, e.g. +91XXXXXXXXXX).");
  if (!to) return;
  try {
    const r = await api(`/api/calls/phone/${customerId}`, { method: "POST", body: JSON.stringify({ to_number: to, force: true }) });
    active = { callId: r.call_id, customerId, poll: setInterval(pollCall, 2000), seenEvents: 0 };
    $("#transcript").innerHTML = ""; $("#outcome").hidden = true;
    $("#call-meta").textContent = `Dialling ${to}… (call ${r.call_id}). Transcript below comes from the server webhook.`;
    phonePoll();
  } catch (e) { alert("Could not place call: " + e.message); }
}

async function phonePoll() {
  // phone calls have no SDK stream, so render transcript lines from server events too
  const id = active.callId; if (!id) return;
  const timer = setInterval(async () => {
    if (active.callId !== id) return clearInterval(timer);
    try {
      const k = await api(`/api/calls/${id}`);
      const lines = (k.events || []).filter((e) => e.kind === "transcript");
      const have = $("#transcript").querySelectorAll(".line.assistant, .line.user").length;
      lines.slice(have).forEach((e) => addLine(e.role, e.text));
    } catch (e) { /* not yet */ }
  }, 2000);
}

// ---------------------------------------------------------------- wiring

document.addEventListener("click", async (e) => {
  const c = e.target.closest("button[data-cact]");
  if (c) {
    try {
      await api(`/api/customers/${c.dataset.id}/customer-action`, { method: "POST", body: JSON.stringify({ action: c.dataset.cact }) });
    } catch (err) { alert(err.message); }
    await renderPersona(c.dataset.id); refresh();
    return;
  }
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const id = b.dataset.id;
  if (b.dataset.act === "review") return reviewCall(id);
  if (b.dataset.act === "web") startWebCall(id);
  if (b.dataset.act === "phone") startPhoneCall(id);
  if (b.dataset.act === "linkpaid") { await api(`/api/customers/${id}/simulate-link-payment`, { method: "POST" }); refresh(); }
});
$("#hangup-btn").addEventListener("click", () => vapi && vapi.stop());
$("#mute-btn").addEventListener("click", () => { if (!vapi) return; const m = !vapi.isMuted(); vapi.setMuted(m); $("#mute-btn").textContent = m ? "Unmute" : "Mute"; });
$("#reset-btn").addEventListener("click", async () => { if (confirm("Reset all customer state and call history?")) { await api("/api/reset", { method: "POST" }); refresh(); } });
$("#plan-btn").addEventListener("click", async () => {
  const p = await api("/api/campaign/plan", { method: "POST" });
  $("#plan-out").hidden = false;
  $("#plan-out").textContent = `Campaign plan (window ${p.window}):\n` + p.plan.map((r) => `${r.dial ? "DIAL " : "SKIP "} ${r.name} — ${r.reason}`).join("\n");
});

(async () => {
  cfg = await api("/api/config"); renderChips(); await refresh(); setInterval(refresh, 8000);
  const review = new URLSearchParams(location.search).get("call");   // deep link: /?call=<call id>
  if (review) reviewCall(review, false).catch(() => {});
})();
