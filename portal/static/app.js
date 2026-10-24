/* NetDevOps Portal.
 *
 * All network rules live on the server, in the same validate.py the
 * pipeline runs. This file draws things and asks the server questions -
 * it never decides whether a change is valid.
 */

const $ = (id) => document.getElementById(id);

let CURRENT = null;      // intent as committed
let OPERATION = null;    // the change the user has described
let LAST_VALID = false;

const api = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try { payload = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = payload && payload.detail;
    throw Object.assign(new Error(typeof detail === "string" ? detail : "Request failed"), { detail, status: response.status });
  }
  return payload;
};

/* ---------------------------------------------------------------- */
/* Sign in                                                           */
/* ---------------------------------------------------------------- */

async function boot() {
  const state = await api("/api/session");
  if (state.auth_required && !state.signed_in) {
    $("login").classList.remove("hidden");
    return;
  }
  await start(state);
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").classList.add("hidden");
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ token: $("token").value }) });
    $("login").classList.add("hidden");
    await start(await api("/api/session"));
  } catch {
    $("login-error").textContent = "That token was not accepted.";
    $("login-error").classList.remove("hidden");
  }
});

async function start(state) {
  $("app").classList.remove("hidden");
  if (!state.github_configured) {
    $("submit-note").textContent =
      "GitHub is not configured on this server, so Submit is unavailable. Set GITHUB_TOKEN and GITHUB_REPO.";
  }
  await load();
}

/* ---------------------------------------------------------------- */
/* Load and draw the current network                                 */
/* ---------------------------------------------------------------- */

async function load() {
  const data = await api("/api/network");
  CURRENT = data.intent;

  drawDiagram(data.summary);
  drawChecks(data.validation);
  drawDevices(data.summary);
  drawLinks(data.summary);
  fillDeviceSelects();
  $("raw-yaml").value = data.intent_text;

  const ok = data.validation.ok;
  const pill = $("validity");
  pill.textContent = ok ? "intent valid" : "intent invalid";
  pill.className = "pill " + (ok ? "ok" : "bad");
  $("counts").textContent =
    `${data.summary.devices.length} devices · ${data.summary.links.length} links`;
}

function drawDiagram(summary) {
  const devices = summary.devices;
  const n = devices.length;
  const W = 720, H = 330, cx = W / 2, cy = H / 2 + 4;
  const radius = Math.min(W, H) / 2 - 62;
  const R = 32;                       // node radius
  const at = {};

  devices.forEach((device, i) => {
    const angle = (i / n) * 2 * Math.PI - Math.PI / 2;
    at[device.name] = { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
  });

  const edges = summary.links.map((link) => {
    const a = at[link.a.device], b = at[link.b.device];
    if (!a || !b) return "";

    // Stop the line at the edge of each circle rather than its centre,
    // and push the label off the line so it never sits on top of it.
    const dx = b.x - a.x, dy = b.y - a.y;
    const length = Math.hypot(dx, dy) || 1;
    const ux = dx / length, uy = dy / length;
    const x1 = a.x + ux * R, y1 = a.y + uy * R;
    const x2 = b.x - ux * R, y2 = b.y - uy * R;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const offset = 11;

    return `<line class="edge" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>
            <text class="edge-label" x="${(mx - uy * offset).toFixed(1)}" y="${(my + ux * offset + 3).toFixed(1)}">${esc(link.subnet)}</text>`;
  }).join("");

  const nodes = devices.map((device) => {
    const p = at[device.name];
    // Labels sit radially outward from the centre, so they move away
    // from the diagram rather than across it.
    const outward = p.y >= cy ? 1 : -1;
    const labelY = outward > 0 ? p.y + R + 17 : p.y - R - 21;
    return `<circle class="node-circle" cx="${p.x}" cy="${p.y}" r="${R}"/>
            <text class="node-label" x="${p.x}" y="${p.y - 2}">${esc(device.name)}</text>
            <text class="node-sub" x="${p.x}" y="${p.y + 13}">AS${esc(device.asn)}</text>
            <text class="node-sub" x="${p.x}" y="${labelY}">${esc(device.loopback)}</text>`;
  }).join("");

  $("diagram").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Network topology">${edges}${nodes}</svg>`;
}

function drawChecks(validation) {
  const rows = validation.checks.map((check) =>
    `<div class="check"><span class="mark">✓</span><span class="name">${esc(check.name)}</span><span class="muted">${esc(check.summary)}</span></div>`
  ).join("");

  const problems = [
    ...validation.errors.map((e) => `<div class="msg error">[${esc(e.check)}] ${esc(e.message)}</div>`),
    ...validation.warnings.map((w) => `<div class="msg warn">[${esc(w.check)}] ${esc(w.message)}</div>`),
  ].join("");

  $("checks").innerHTML = rows + problems;
}

function drawDevices(summary) {
  $("device-list").innerHTML = summary.devices.map((device) => `
    <div class="device">
      <div class="device-head">
        <span class="name">${esc(device.name)}</span>
        <span class="badge">AS${esc(device.asn)}</span>
        <span class="muted small mono">${esc(device.loopback)}</span>
      </div>
      <table class="grid">
        <thead><tr><th>Interface</th><th>Address</th><th>Neighbour</th></tr></thead>
        <tbody>${device.interfaces.map((intf, i) => `
          <tr>
            <td>${esc(intf.name)}</td>
            <td>${esc(intf.address)}</td>
            <td>${esc(intf.peer)}${device.neighbors[i] ? ` <span class="muted">via ${esc(device.neighbors[i].address)}</span>` : ""}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`).join("");
}

function drawLinks(summary) {
  $("link-table").querySelector("tbody").innerHTML = summary.links.map((link) => `
    <tr>
      <td>${esc(link.a.device)}:${esc(link.a.interface)}</td>
      <td>${esc(link.b.device)}:${esc(link.b.interface)}</td>
      <td>${esc(link.subnet)}</td>
    </tr>`).join("");
}

function fillDeviceSelects() {
  const names = Object.keys(CURRENT.devices).sort();
  const options = names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
  $("link-a-device").innerHTML = options;
  $("link-b-device").innerHTML = options;
  if (names.length > 1) $("link-b-device").value = names[1];
}

/* ---------------------------------------------------------------- */
/* Tabs and change kinds                                             */
/* ---------------------------------------------------------------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
    $("view-" + tab.dataset.view).classList.remove("hidden");
  });
});

document.querySelectorAll(".kind").forEach((kind) => {
  kind.addEventListener("click", () => {
    document.querySelectorAll(".kind").forEach((k) => k.classList.remove("active"));
    kind.classList.add("active");
    ["link", "device", "raw"].forEach((name) =>
      $("form-" + name).classList.toggle("hidden", name !== kind.dataset.kind));
  });
});

const activeKind = () => document.querySelector(".kind.active").dataset.kind;

/* ---------------------------------------------------------------- */
/* Describe the change as an operation                               */
/* ---------------------------------------------------------------- */

/* The server applies this to the committed file as text, so comments
 * and formatting survive. Sending a rewritten document instead would
 * strip every comment in intent/network.yml. */
function buildOperation() {
  const kind = activeKind();

  if (kind === "raw") {
    return { kind: "raw", yaml: $("raw-yaml").value };
  }

  if (kind === "link") {
    const op = {
      kind: "add_link",
      a_device: $("link-a-device").value,
      a_interface: $("link-a-intf").value.trim(),
      b_device: $("link-b-device").value,
      b_interface: $("link-b-intf").value.trim(),
      subnet: $("link-subnet").value.trim(),
    };
    if (!op.a_interface || !op.b_interface || !op.subnet) {
      throw new Error("Fill in both interfaces and the subnet.");
    }
    if (!$("pr-title").value) {
      $("pr-title").value = `Add link between ${op.a_device} and ${op.b_device}`;
    }
    return op;
  }

  const op = {
    kind: "add_device",
    name: $("dev-name").value.trim(),
    asn: parseInt($("dev-asn").value, 10),
    loopback: $("dev-loopback").value.trim(),
  };
  if (!op.name || !op.loopback || Number.isNaN(op.asn)) {
    throw new Error("Fill in the name, AS number and loopback.");
  }
  if (!$("pr-title").value) $("pr-title").value = `Add device ${op.name}`;
  return op;
}

/* ---------------------------------------------------------------- */
/* Check and submit                                                  */
/* ---------------------------------------------------------------- */

$("apply").addEventListener("click", async () => {
  const result = $("result");
  $("diff-area").classList.add("hidden");
  $("submit-area").classList.add("hidden");
  LAST_VALID = false;

  try {
    OPERATION = buildOperation();
  } catch (error) {
    result.innerHTML = `<div class="msg error">${esc(error.message)}</div>`;
    return;
  }

  result.textContent = "Checking\u2026";

  let outcome;
  try {
    outcome = await api("/api/propose", {
      method: "POST",
      body: JSON.stringify({ operation: OPERATION }),
    });
  } catch (error) {
    result.innerHTML = `<div class="msg error">${esc(error.message)}</div>`;
    return;
  }

  const validation = outcome.validation;

  if (!validation.ok) {
    result.innerHTML =
      `<div class="msg error">Rejected in ${validation.seconds}s \u2014 nothing was built.</div>` +
      validation.errors.map((e) => `<div class="msg error">[${esc(e.check)}] ${esc(e.message)}</div>`).join("");
    return;
  }

  LAST_VALID = true;
  result.innerHTML =
    `<div class="msg good">Valid \u2014 checked in ${validation.seconds}s, before anything was built.</div>` +
    validation.warnings.map((w) => `<div class="msg warn">[${esc(w.check)}] ${esc(w.message)}</div>`).join("");

  $("diffs").innerHTML = outcome.changes.length
    ? outcome.changes.map(renderDiff).join("")
    : `<p class="muted">This change produces no difference.</p>`;
  $("diff-area").classList.remove("hidden");

  if (outcome.changes.length) $("submit-area").classList.remove("hidden");
});

function renderDiff(change) {
  const body = change.diff.split("\n").map((line) => {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add"
              : line.startsWith("-") && !line.startsWith("---") ? "del"
              : (line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---")) ? "meta" : "";
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join("\n");

  return `<div class="diff-file">
    <div class="diff-head"><span>${esc(change.path)}</span><span class="badge">${esc(change.status)}</span></div>
    <pre class="diff-body">${body}</pre>
  </div>`;
}

$("submit").addEventListener("click", async () => {
  if (!LAST_VALID) return;
  const button = $("submit");
  button.disabled = true;
  button.textContent = "Opening pull request…";

  try {
    const pr = await api("/api/submit", {
      method: "POST",
      body: JSON.stringify({
        operation: OPERATION,
        title: $("pr-title").value,
        description: $("pr-body").value,
      }),
    });
    $("result").innerHTML =
      `<div class="msg good">Pull request #${pr.number} opened on branch <code>${esc(pr.branch)}</code>.
       The pipeline is now building the topology and running the tests.
       <a href="${esc(pr.url)}" target="_blank" rel="noopener">Open it on GitHub →</a></div>`;
    $("diff-area").classList.add("hidden");
    $("submit-area").classList.add("hidden");
  } catch (error) {
    const detail = error.detail;
    const message = typeof detail === "string" ? detail : (detail && detail.message) || error.message;
    const errors = (detail && detail.errors) || [];
    $("result").innerHTML =
      `<div class="msg error">${esc(message)}</div>` +
      errors.map((e) => `<div class="msg error">[${esc(e.check)}] ${esc(e.message)}</div>`).join("");
  } finally {
    button.disabled = false;
    button.textContent = "Submit for review";
  }
});

$("reset").addEventListener("click", async () => {
  ["link-a-intf", "link-b-intf", "link-subnet", "dev-name", "dev-asn",
   "dev-loopback", "pr-title", "pr-body"].forEach((id) => { $(id).value = ""; });
  $("diff-area").classList.add("hidden");
  $("submit-area").classList.add("hidden");
  $("result").textContent = "Make a change above, then check it.";
  LAST_VALID = false;
  await load();
});

/* ---------------------------------------------------------------- */

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot();
