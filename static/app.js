/* =============================================================
   API Capture Tool — app.js
   Single-page 4-stage UI controller
   ============================================================= */

"use strict";

// ---------------------------------------------------------------
// App State
// ---------------------------------------------------------------
const App = {
  stage: 1,
  testCases: [],
  capturedFlows: [],
  mappings: [],       // [{tc, api_id, confidence, reason}]
  mappingWarning: "",
  llmStatus: null,
  presets: {},
  activePresetKey: "standard_ignore_list",
  rowState: {},       // keyed by tc_number: {flowId, commonIgnore: [...], correctedStatus, manuallySelected}
  appendMode: false,
  startFromCase: 1,
};

// ---------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------
function qs(sel, ctx) { return (ctx || document).querySelector(sel); }
function qsa(sel, ctx) { return Array.from((ctx || document).querySelectorAll(sel)); }
function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => {
    if (k === "class") e.className = v;
    else if (k.startsWith("data-")) e.dataset[k.slice(5)] = v;
    else e[k] = v;
  });
  children.forEach(c => {
    if (typeof c === "string") e.appendChild(document.createTextNode(c));
    else if (c) e.appendChild(c);
  });
  return e;
}

function showBanner(selector, title, message) {
  const banner = qs(selector);
  if (!banner) return;
  banner.textContent = "";
  const strong = document.createElement("strong");
  strong.textContent = title;
  banner.appendChild(strong);
  banner.appendChild(document.createTextNode(" " + message));
  banner.classList.remove("hidden");
}

function hideBanner(selector) {
  const banner = qs(selector);
  if (banner) banner.classList.add("hidden");
}

// ---------------------------------------------------------------
// Stage transition
// ---------------------------------------------------------------
function goToStage(n) {
  App.stage = n;
  qsa(".stage").forEach(s => s.classList.add("hidden"));
  const target = qs(`#stage-${n}`);
  if (target) target.classList.remove("hidden");

  // Update pills
  qsa(".stage-pill").forEach(p => {
    const pn = parseInt(p.dataset.stage, 10);
    p.classList.remove("active", "done");
    if (pn < n) p.classList.add("done");
    else if (pn === n) p.classList.add("active");
  });
}

// ---------------------------------------------------------------
// Append mode — check existing API files
// ---------------------------------------------------------------
async function _checkExistingApi(apiName) {
  const panel   = qs("#append-confirm-panel");
  const errMsg  = qs("#append-error-msg");
  const checkbox = qs("#append-mode");

  if (!apiName) {
    if (panel)  panel.style.display = "none";
    if (errMsg) { errMsg.style.display = "none"; errMsg.textContent = ""; }
    return false;
  }

  try {
    const res  = await fetch(`/api/check-existing-api?api_name=${encodeURIComponent(apiName)}`);
    const data = await res.json();

    if (data.found) {
      const counts = data.entry_counts || {};
      const files  = data.files || {};
      const fileList = qs("#append-file-list");
      fileList.innerHTML = "";
      [
        ["Payload JSON",   files.payload,  counts.payload],
        ["Expected JSON",  files.expected, counts.expected],
        ["apiInputs CSV",  files.inputs,   counts.inputs],
      ].forEach(([label, path, count]) => {
        const li = document.createElement("li");
        li.textContent = `${label}: ${path} (${count} existing entr${count === 1 ? "y" : "ies"})`;
        fileList.appendChild(li);
      });

      // Auto-populate start-from-case = max existing count + 1
      const maxCount = Math.max(counts.payload || 0, counts.expected || 0, counts.inputs || 0);
      const startInput = qs("#start-from-case");
      if (startInput) startInput.value = maxCount + 1;

      if (panel)  panel.style.display = "block";
      if (errMsg) { errMsg.style.display = "none"; errMsg.textContent = ""; }
      return true;
    } else {
      const missing = (data.missing || []).join(", ");
      if (errMsg) {
        errMsg.textContent = "JSON files with same API name not found — this scenario isn’t supported now."
          + (missing ? ` (missing: ${missing})` : "");
        errMsg.style.display = "block";
      }
      if (panel)    panel.style.display = "none";
      if (checkbox) checkbox.checked = false;
      return false;
    }
  } catch (e) {
    if (errMsg) {
      errMsg.textContent = "Failed to check existing files: " + e.message;
      errMsg.style.display = "block";
    }
    if (checkbox) checkbox.checked = false;
    return false;
  }
}

// ---------------------------------------------------------------
// Stage 1 — Setup
// ---------------------------------------------------------------
// ---------------------------------------------------------------
// Early Automation — spec content holder
// ---------------------------------------------------------------
let _specContent = null;

function initStage1() {
  const earlyAutomation = qs("#early-automation");
  const captureHostInput = qs("#capture-host");
  const specSection = qs("#spec-upload-section");
  const btnStart = qs("#btn-start-capture");

  checkLlmStatus();

  earlyAutomation.addEventListener("change", () => {
    captureHostInput.disabled = earlyAutomation.checked;
    specSection.style.display = earlyAutomation.checked ? "block" : "none";
    btnStart.textContent = earlyAutomation.checked ? "Analyse & Map" : "Start Capture";
    // Mutually exclusive with append mode
    if (earlyAutomation.checked) {
      const appendCb = qs("#append-mode");
      if (appendCb && appendCb.checked) {
        appendCb.checked = false;
        const p = qs("#append-confirm-panel"); if (p) p.style.display = "none";
        const e = qs("#append-error-msg"); if (e) { e.style.display = "none"; e.textContent = ""; }
      }
    }
  });

  // Append mode — checkbox handler
  const appendModeCb = qs("#append-mode");
  if (appendModeCb) {
    appendModeCb.addEventListener("change", async () => {
      if (!appendModeCb.checked) {
        const p = qs("#append-confirm-panel"); if (p) p.style.display = "none";
        const e = qs("#append-error-msg"); if (e) { e.style.display = "none"; e.textContent = ""; }
        return;
      }
      // Mutually exclusive with early automation
      if (earlyAutomation.checked) {
        earlyAutomation.checked = false;
        captureHostInput.disabled = false;
        specSection.style.display = "none";
        btnStart.textContent = "Start Capture";
      }
      const apiName = qs("#api-name").value.trim();
      if (!apiName) {
        alert("Please enter the API Name first, then tick this checkbox.");
        appendModeCb.checked = false;
        return;
      }
      await _checkExistingApi(apiName);
    });
  }

  // Re-validate when API name changes while append mode is active
  qs("#api-name").addEventListener("blur", async () => {
    const appendCb = qs("#append-mode");
    if (appendCb && appendCb.checked) {
      await _checkExistingApi(qs("#api-name").value.trim());
    }
  });

  // Spec file reader
  qs("#spec-file").addEventListener("change", async () => {
    const file = qs("#spec-file").files[0];
    if (!file) return;
    try {
      _specContent = await file.text();
      qs("#spec-upload-hint").style.display = "none";
      const confirm = qs("#spec-upload-confirm");
      confirm.textContent = `✓ ${file.name} loaded`;
      confirm.style.display = "block";
    } catch (e) {
      _specContent = null;
    }
  });

  const fileInput = qs("#csv-file");
  const uploadZone = qs("#upload-zone");
  const uploadHint = qs("#upload-hint");
  const uploadConfirm = qs("#upload-confirm");

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await fetch("/api/upload-csv", { method: "POST", body: fd });
      const data = await res.json();
      App.testCases = data.test_cases || [];
      uploadHint.style.display = "none";
      uploadConfirm.style.display = "block";
      uploadConfirm.textContent = `${App.testCases.length} test cases loaded from ${file.name}`;
      // Pre-fill API name from filename (editable)
      const apiNameInput = qs("#api-name");
      if (apiNameInput) apiNameInput.value = file.name.replace(/\.csv$/i, "");
    } catch (e) {
      uploadConfirm.style.display = "block";
      uploadConfirm.style.color = "#ef4444";
      uploadConfirm.textContent = "Failed to parse CSV: " + e.message;
    }
  });

  qs("#btn-start-capture").addEventListener("click", async () => {
    const apiName = qs("#api-name").value.trim();
    const module = qs("#module").value.trim();
    const manifest = qs("#manifest").value;
    const owner = qs("#owner").value.trim();
    const captureHost = qs("#capture-host").value.trim();
    const businessFunction = qs("#business-function").value.trim();

    if (!apiName) { alert("Please enter an API Name."); return; }
    if (!App.testCases.length) { alert("Please upload a CSV first."); return; }

    // Capture append mode + auth settings before potentially filtering App.testCases
    const isAppendMode = !!(qs("#append-mode") && qs("#append-mode").checked);
    const startFromCase = isAppendMode ? (parseInt((qs("#start-from-case") || {}).value) || 1) : 1;
    const authModeEl = document.querySelector("input[name='auth-mode']:checked");
    const authMode = authModeEl ? authModeEl.value : "auto";
    App.appendMode    = isAppendMode;
    App.startFromCase = startFromCase;
    App.authMode      = authMode;

    // Filter test cases to only the new ones when appending
    if (isAppendMode && startFromCase > 1) {
      App.testCases = App.testCases.filter(tc => {
        const num = parseInt(tc.testScenarioNumber || 0, 10);
        return num >= startFromCase;
      });
    }

    const btn = qs("#btn-start-capture");

    if (qs("#early-automation").checked) {
      // ---- Early Automation path ----
      btn.disabled = true;
      btn.textContent = "Analysing...";
      try {
        const res = await fetch("/api/early-start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            api_name: apiName, module, manifest, owner,
            business_function: businessFunction,
            capture_host: captureHost,
            spec_content: _specContent,
          }),
        });
        const data = await res.json();
        if (data.ok) {
          App.earlyMode = true;
          App.mappingWarning = "";
          App.earlySuggestions = data.suggestions || [];
          App.presets = data.presets || App.presets;
          _initEarlyRowState(data.suggestions || []);
          buildStage3Early();
          buildStage3();   // also init normal stage3 listeners (preset/confirm)
          goToStage(3);
        } else {
          alert("Analysis failed: " + (data.error || "unknown error"));
          btn.disabled = false;
          btn.textContent = "Analyse & Map";
        }
      } catch (e) {
        alert("Error: " + e.message);
        btn.disabled = false;
        btn.textContent = "Analyse & Map";
      }
    } else {
      // ---- Normal capture path (unchanged) ----
      btn.disabled = true;
      btn.textContent = "Starting...";
      App.earlyMode = false;
      App.mappingWarning = "";
      try {
        const res = await fetch("/api/start-capture", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_name: apiName, module, manifest, owner, capture_host: captureHost, append_mode: isAppendMode, start_from_case: startFromCase, auth_mode: authMode }),
        });
        const data = await res.json();
        if (data.ok) {
          const warning = startCaptureWarning(data);
          if (warning) alert(warning);
          goToStage(2);
          startPolling();
        } else {
          alert("Failed to start capture: " + (data.error || "unknown error"));
          btn.disabled = false;
          btn.textContent = "Start Capture";
        }
      } catch (e) {
        alert("Error: " + e.message);
        btn.disabled = false;
        btn.textContent = "Start Capture";
      }
    }
  });
}

function startCaptureWarning(data) {
  if (data.ok && data.proxy_ok === false) {
    return "Capture started, but the proxy may not be running correctly " +
      "(mitmdump may have failed to launch — check if port 8080 is already " +
      "in use). You may see 0 requests captured even after clicking around " +
      "in the capture window.";
  }
  return null;
}

async function checkLlmStatus() {
  try {
    const res = await fetch("/api/llm-status");
    const data = await res.json();
    App.llmStatus = data;
    if (data && !data.ok) {
      showBanner("#llm-status-banner", "Auto-mapping unavailable.", data.message || "LLM auto-mapping is not configured. Manual mapping is still available.");
    } else {
      hideBanner("#llm-status-banner");
    }
  } catch (e) {
    showBanner("#llm-status-banner", "Auto-mapping status unknown.", "Could not check LLM configuration. Manual mapping is still available if auto-mapping fails.");
  }
}

// ---------------------------------------------------------------
// Stage 2 — Capturing
// ---------------------------------------------------------------
let _pollInterval = null;

function startPolling() {
  if (_pollInterval) clearInterval(_pollInterval);
  _pollInterval = setInterval(async () => {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      const countEl = qs("#capture-count");
      countEl.textContent = `${data.flow_count} request${data.flow_count === 1 ? "" : "s"} captured`;
    } catch (e) {
      // ignore polling errors
    }
  }, 2000);
}

function stopPolling() {
  if (_pollInterval) { clearInterval(_pollInterval); _pollInterval = null; }
}

function initStage2() {
  qs("#btn-stop-capture").addEventListener("click", async () => {
    stopPolling();
    const btn = qs("#btn-stop-capture");
    btn.disabled = true;
    qs("#stop-spinner").classList.remove("hidden");

    try {
      const res = await fetch("/api/stop-capture", { method: "POST" });
      const data = await res.json();
      App.capturedFlows = data.flows || [];
      App.mappings = data.mappings || [];
      App.mappingWarning = data.mapping_warning || "";
      App.presets = data.presets || {};

      buildStage3();
      goToStage(3);
    } catch (e) {
      alert("Error stopping capture: " + e.message);
      btn.disabled = false;
      qs("#stop-spinner").classList.add("hidden");
    }
  });
}

// ---------------------------------------------------------------
// Early Automation — row state init
// ---------------------------------------------------------------
function _defaultStatus(method) {
  const m = (method || "").toUpperCase();
  if (m === "POST") return 201;
  if (m === "DELETE") return 200;
  return 200;
}

function _initEarlyRowState(suggestions) {
  const defaultPreset = App.presets[App.activePresetKey] || [];
  suggestions.forEach(s => {
    const tcNum = s.tc;
    App.rowState[tcNum] = {
      // Early mode fields
      method: (s.method || "GET").toUpperCase(),
      mappedApi: s.path || "",
      requestBody: JSON.stringify(s.request_body || {}, null, 2),
      responseBody: JSON.stringify(s.response_body || {}, null, 2),
      eaSource: s.source || null,
      linkedGetTc: s.linked_get_tc || null,
      linkedPostTc: s.linked_post_tc || null,
      // Shared fields
      correctedStatus: s.suggested_status || _defaultStatus(s.method),
      commonIgnore: [...defaultPreset],
      testStresser: "N",
      tcObjective: "",
    };
  });
  // Also populate tcObjective from App.testCases
  App.testCases.forEach((tc, idx) => {
    const num = parseInt(tc.testScenarioNumber || (idx + 1), 10);
    if (App.rowState[num]) {
      App.rowState[num].tcObjective = tc.testObjective || "";
    }
  });
}

// ---------------------------------------------------------------
// Early Automation — build Stage 3 table
// ---------------------------------------------------------------
let _eaDebounceTimers = {};

function buildStage3Early() {
  // Show EA table, hide normal table
  const eaCard = qs("#ea-mapping-card");
  const normalCard = qs("#mapping-card");
  if (eaCard) eaCard.style.display = "block";
  if (normalCard) normalCard.style.display = "none";

  const tbody = qs("#ea-mapping-tbody");
  tbody.innerHTML = "";

  App.testCases.forEach((tc, idx) => {
    const tcNum = parseInt(tc.testScenarioNumber || (idx + 1), 10);
    const state = App.rowState[tcNum] || {};
    const source = state.eaSource;
    const isGet = (state.method || "").toUpperCase() === "GET";

    // Source badge
    const badgeCls = source === "spec" ? "ea-badge-spec"
                   : source === "existing" ? "ea-badge-existing"
                   : "ea-badge-manual";
    const badgeLabel = source === "spec" ? "SPEC"
                     : source === "existing" ? "EXISTING"
                     : "MANUAL";

    // Determine body highlight class
    const bodyClass = source === "existing" ? "ea-existing"
                    : source === "spec" ? "ea-draft"
                    : "";

    // ---- Main row ----
    const tr = document.createElement("tr");
    tr.dataset.tc = tcNum;
    tr.innerHTML = `
      <td style="white-space:nowrap">${tcNum}</td>
      <td>
        <div class="tc-objective">${escHtml(tc.testObjective || "")}</div>
        <div class="expand-toggle" data-tc="${tcNum}">▶ Show details</div>
      </td>
      <td>
        <select class="select-input ea-method-select" data-tc="${tcNum}" style="padding:5px 8px;font-size:.84rem;width:100%">
          ${["GET","POST","PUT","PATCH","DELETE"].map(m =>
            `<option value="${m}"${state.method === m ? " selected" : ""}>${m}</option>`
          ).join("")}
        </select>
      </td>
      <td>
        <div style="position:relative">
          <input type="text" class="text-input ea-path-input" data-tc="${tcNum}"
            value="${escHtml(state.mappedApi || "")}"
            placeholder="/api/v1/..."
            style="width:100%;font-size:.82rem;padding:7px 10px">
          <span class="ea-sync-label" id="ea-sync-${tcNum}" style="display:none;position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:.7rem;color:var(--yellow);font-weight:700">Syncing…</span>
        </div>
      </td>
      <td><span class="ea-badge ${badgeCls}">${badgeLabel}</span></td>
      <td id="status-cell-${tcNum}">
        <input type="number" class="status-input" data-tc="${tcNum}" value="${state.correctedStatus || 200}" style="width:70px">
      </td>
      <td>
        <select class="select-input stresser-select" data-tc="${tcNum}" style="padding:5px 8px;font-size:.82rem;width:100%">
          <option value="Y">Y</option>
          <option value="N" selected>N</option>
        </select>
      </td>
    `;

    // ---- Detail row ----
    const detailTr = document.createElement("tr");
    detailTr.className = "detail-row";
    detailTr.style.display = "none";
    detailTr.innerHTML = `
      <td colspan="7" class="detail-cell">
        <div class="detail-inner">
          <div class="detail-col">
            <div class="detail-label">Request Body ${source === "spec" ? '<span style="color:#ef4444;font-size:.7rem;font-weight:700;margin-left:6px">● fill placeholders</span>' : source === "existing" ? '<span style="color:#10b981;font-size:.7rem;font-weight:700;margin-left:6px">● from suite</span>' : ''}</div>
            <textarea class="ea-body-textarea ${bodyClass}" id="req-${tcNum}" data-tc="${tcNum}" data-field="request"
              style="width:100%;min-height:130px;font-family:monospace;font-size:.78rem;padding:8px;border-radius:6px;resize:vertical"
              placeholder="Request body JSON">${escHtml(isGet ? "" : (state.requestBody || ""))}</textarea>
            <div class="detail-label" style="margin-top:10px">Response Body ${source === "spec" ? '<span style="color:#ef4444;font-size:.7rem;font-weight:700;margin-left:6px">● fill placeholders</span>' : source === "existing" ? '<span style="color:#10b981;font-size:.7rem;font-weight:700;margin-left:6px">● from suite</span>' : ''}</div>
            <textarea class="ea-body-textarea ${bodyClass}" id="resp-${tcNum}" data-tc="${tcNum}" data-field="response"
              style="width:100%;min-height:130px;font-family:monospace;font-size:.78rem;padding:8px;border-radius:6px;resize:vertical"
              placeholder="Expected response JSON">${escHtml(state.responseBody || "")}</textarea>
          </div>
          <div class="detail-col">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <div class="detail-label" style="margin-bottom:0">commonIgnore</div>
              <select class="select-input ci-preset-select" data-tc="${tcNum}" style="padding:4px 8px;font-size:.76rem;width:auto;min-width:150px">
                <option value="standard_ignore_list">Standard Preset</option>
                <option value="no_commonignore">No CommonIgnore</option>
              </select>
            </div>
            <div class="ci-tags" id="ci-tags-${tcNum}"></div>
            <div class="ci-add-form">
              <input type="text" class="ci-add-input" id="ci-add-input-${tcNum}" placeholder="field name">
              <button class="ci-add-btn" data-tc="${tcNum}">+ Add</button>
            </div>
          </div>
        </div>
      </td>
    `;

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);
    refreshCiTags(tcNum);

    // Expand toggle
    tr.querySelector(".expand-toggle").addEventListener("click", () => {
      const isOpen = detailTr.style.display !== "none";
      detailTr.style.display = isOpen ? "none" : "table-row";
      tr.querySelector(".expand-toggle").textContent = isOpen ? "▶ Show details" : "▼ Hide details";
    });

    // Status input
    tr.querySelector(".status-input").addEventListener("change", e => {
      App.rowState[tcNum].correctedStatus = parseInt(e.target.value, 10);
    });

    // Stresser select
    tr.querySelector(".stresser-select").addEventListener("change", e => {
      App.rowState[tcNum].testStresser = e.target.value;
    });

    // Method select — toggle request body visibility
    tr.querySelector(".ea-method-select").addEventListener("change", e => {
      const newMethod = e.target.value;
      App.rowState[tcNum].method = newMethod;
      const reqTextarea = qs(`#req-${tcNum}`);
      if (newMethod === "GET") {
        if (reqTextarea) reqTextarea.style.opacity = "0.4";
      } else {
        if (reqTextarea) reqTextarea.style.opacity = "1";
        _triggerGetSync(tcNum, newMethod);
      }
    });

    // Path input — on change, sync linked GET
    tr.querySelector(".ea-path-input").addEventListener("input", e => {
      App.rowState[tcNum].mappedApi = e.target.value;
      _triggerGetSync(tcNum, App.rowState[tcNum].method);
    });

    // Body textareas — track edits, remove draft highlight
    detailTr.querySelectorAll(".ea-body-textarea").forEach(ta => {
      ta.addEventListener("input", e => {
        const field = e.target.dataset.field;
        if (field === "request") {
          App.rowState[tcNum].requestBody = e.target.value;
          e.target.classList.remove("ea-draft", "ea-existing", "ea-amber");
        } else {
          App.rowState[tcNum].responseBody = e.target.value;
          e.target.classList.remove("ea-draft", "ea-existing", "ea-amber");
        }
        _triggerGetSync(tcNum, App.rowState[tcNum].method);
      });
    });

    // commonIgnore
    detailTr.querySelector(".ci-preset-select").addEventListener("change", e => {
      const preset = App.presets[e.target.value] || [];
      App.rowState[tcNum].commonIgnore = [...preset];
      refreshCiTags(tcNum);
    });
    detailTr.querySelector(".ci-add-btn").addEventListener("click", () => {
      const inp = qs(`#ci-add-input-${tcNum}`);
      const val = inp.value.trim();
      if (val && !App.rowState[tcNum].commonIgnore.includes(val)) {
        App.rowState[tcNum].commonIgnore.push(val);
        refreshCiTags(tcNum);
      }
      inp.value = "";
    });

    // Dim request body if GET
    if (isGet) {
      const reqTextarea = qs(`#req-${tcNum}`);
      if (reqTextarea) reqTextarea.style.opacity = "0.4";
    }
  });
}

function _triggerGetSync(postTcNum, method) {
  if ((method || "").toUpperCase() === "GET") return;
  const postState = App.rowState[postTcNum];
  if (!postState || !postState.linkedGetTc) return;
  const getTcNum = postState.linkedGetTc;

  // Clear existing debounce
  if (_eaDebounceTimers[postTcNum]) clearTimeout(_eaDebounceTimers[postTcNum]);

  _eaDebounceTimers[postTcNum] = setTimeout(async () => {
    // Show syncing label on GET row
    const syncLabel = qs(`#ea-sync-${getTcNum}`);
    if (syncLabel) syncLabel.style.display = "inline";

    const getTc = App.testCases.find(tc =>
      parseInt(tc.testScenarioNumber, 10) === getTcNum
    ) || { testObjective: "" };

    try {
      const res = await fetch("/api/early-update-get", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          post_data: {
            path: postState.mappedApi,
            method: postState.method,
            request_body: _safeParseJson(postState.requestBody),
            response_body: _safeParseJson(postState.responseBody),
          },
          get_tc: { testObjective: getTc.testObjective || "" },
        }),
      });
      const data = await res.json();
      if (data.ok && data.suggestion) {
        const s = data.suggestion;
        // Update GET row state
        App.rowState[getTcNum].mappedApi = s.suggested_path || App.rowState[getTcNum].mappedApi;
        App.rowState[getTcNum].responseBody = JSON.stringify(s.suggested_response || {}, null, 2);

        // Update GET row DOM
        const pathInput = qs(`.ea-path-input[data-tc="${getTcNum}"]`);
        if (pathInput) pathInput.value = App.rowState[getTcNum].mappedApi;

        const respTextarea = qs(`#resp-${getTcNum}`);
        if (respTextarea) {
          respTextarea.value = App.rowState[getTcNum].responseBody;
          respTextarea.classList.remove("ea-draft", "ea-existing");
          respTextarea.classList.add("ea-amber");
        }
        const pathInputEl = qs(`.ea-path-input[data-tc="${getTcNum}"]`);
        if (pathInputEl) {
          pathInputEl.classList.add("ea-amber");
        }
      }
    } catch (_) { /* silent fail */ }

    if (syncLabel) syncLabel.style.display = "none";
  }, 800);
}

function _safeParseJson(str) {
  try { return JSON.parse(str); } catch (_) { return {}; }
}

function _normalizePath(path) {
  return (path || "").replace(/\/\d+/g, "/{id}");
}

// ---------------------------------------------------------------
// Stage 3 — Map & Review
// ---------------------------------------------------------------
function buildStage3() {
  renderMappingWarning();

  // Fetch presets if not already populated
  if (!Object.keys(App.presets).length) {
    fetch("/api/presets").then(r => r.json()).then(p => {
      App.presets = p;
      populatePresetPreview();
    });
  } else {
    populatePresetPreview();
  }

  // Early mode: rowState is already initialized by _initEarlyRowState and the
  // table is built by buildStage3Early() — calling buildMappingTable() here
  // would overwrite rowState with capture-only fields (flowId etc.) and discard
  // the suggested method/path/body values before confirmation.
  if (!App.earlyMode) {
    buildMappingTable();
  }

  qs("#btn-apply-preset").addEventListener("click", () => {
    const key = qs("#preset-select").value;
    App.activePresetKey = key;
    const preset = App.presets[key] || [];
    Object.keys(App.rowState).forEach(tcNum => {
      App.rowState[tcNum].commonIgnore = [...preset];
      refreshCiTags(parseInt(tcNum));
      const rowSel = qs(`.ci-preset-select[data-tc="${tcNum}"]`);
      if (rowSel) rowSel.value = key;
    });
  });

  qs("#preset-select").addEventListener("change", () => {
    const key = qs("#preset-select").value;
    App.activePresetKey = key;
    populatePresetPreview();
    // Sync all per-row dropdowns to match global selection
    qsa(".ci-preset-select").forEach(sel => { sel.value = key; });
  });

  const retryBtn = qs("#btn-retry-mapping");
  if (retryBtn) {
    retryBtn.style.display = App.earlyMode ? "none" : "inline-flex";
    retryBtn.onclick = handleRetryMapping;
  }

  qs("#btn-confirm").addEventListener("click", handleConfirm);
}

function renderMappingWarning() {
  if (App.mappingWarning) {
    showBanner(
      "#mapping-warning",
      "Auto-mapping warning.",
      `${App.mappingWarning} Captured requests are still available below; select the mapped API manually.`
    );
  } else {
    hideBanner("#mapping-warning");
  }
}

function populatePresetPreview() {
  const key = qs("#preset-select").value;
  const fields = (App.presets[key] || []);
  qs("#preset-preview").textContent = fields.length
    ? "Fields: " + fields.join(", ")
    : "(empty)";
}

function existingMappingsForRetry() {
  const byTc = {};

  // Seed from the last mapping pass (auto-mapped or a prior retry) — not
  // just manual picks — so a retry only asks the LLM about test cases that
  // are genuinely still unmapped, instead of re-mapping everything the
  // first pass already got right.
  (App.mappings || []).forEach(m => {
    const tc = parseInt(m.tc, 10);
    const apiId = m.api_id != null ? parseInt(m.api_id, 10) : NaN;
    if (Number.isNaN(tc) || Number.isNaN(apiId)) return;

    const rowState = App.rowState[tc];
    const rowFlowId = rowState && rowState.flowId != null ? parseInt(rowState.flowId, 10) : null;
    // If the row has since diverged from this mapping (edited or cleared
    // manually), defer to rowState below rather than preserving a stale value.
    if (rowFlowId !== null && rowFlowId !== apiId) return;
    if (rowState && rowFlowId === null) return;

    byTc[tc] = {
      tc,
      api_id: apiId,
      confidence: m.confidence ?? 100,
      reason: m.reason || "Preserved from a previous mapping pass.",
    };
  });

  // Manual selections always take precedence over a mapping-pass value.
  Object.entries(App.rowState || {}).forEach(([tcNum, state]) => {
    const tc = parseInt(tcNum, 10);
    const apiId = state && state.flowId != null ? parseInt(state.flowId, 10) : NaN;
    if (state && state.manuallySelected && !Number.isNaN(tc) && !Number.isNaN(apiId)) {
      byTc[tc] = {
        tc,
        api_id: apiId,
        confidence: 100,
        reason: "Manually selected in the UI; preserved during retry mapping.",
      };
    }
  });

  return Object.values(byTc).sort((a, b) => parseInt(a.tc, 10) - parseInt(b.tc, 10));
}

async function handleRetryMapping() {
  if (App.earlyMode) return;

  const btn = qs("#btn-retry-mapping");
  const pauseBtn = qs("#btn-pause-mapping");
  const spinner = qs("#retry-spinner");
  const status = qs("#retry-status");
  const retryFromTc = Math.max(1, parseInt((qs("#retry-from-tc") || {}).value) || 1);
  let retryMappings = existingMappingsForRetry();
  const initiallyPreservedCount = retryMappings.length;
  const batchSize = 50;
  const totalFlows = App.capturedFlows.length;
  const totalBatches = Math.max(1, Math.ceil(totalFlows / batchSize));
  let startIndex = 0;
  let batchesProcessed = 0;
  let lastWarning = "";
  let finalData = null;

  if (btn) btn.disabled = true;
  if (pauseBtn) { pauseBtn.classList.remove("hidden"); pauseBtn.onclick = async () => { await fetch("/api/pause-mapping", { method: "POST" }); if (status) status.textContent = "Pause requested…"; }; }
  if (spinner) spinner.classList.remove("hidden");
  if (status) {
    status.textContent = initiallyPreservedCount
      ? `Preserving ${initiallyPreservedCount} manual row${initiallyPreservedCount === 1 ? "" : "s"}; retrying batch 1/${totalBatches}...`
      : "Retrying in 50-request batches...";
  }

  try {
    while (startIndex < totalFlows) {
      const batchNumber = Math.floor(startIndex / batchSize) + 1;
      const batchEnd = Math.min(totalFlows, startIndex + batchSize);
      if (status) {
        const mapped = retryMappings.filter(m => m.api_id != null).length;
        status.textContent = `Retrying batch ${batchNumber}/${totalBatches} (requests ${startIndex + 1}-${batchEnd}); mapped ${mapped}/${App.testCases.length} so far...`;
      }

      const res = await fetch("/api/retry-mapping", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          existing_mappings: retryMappings,
          start_index: startIndex,
          batch_size: batchSize,
          retry_from_tc: retryFromTc,
        }),
      });
      const data = await res.json();
      if (!data.ok) {
        alert("Retry mapping failed: " + (data.error || "unknown error"));
        if (status) status.textContent = "Retry failed";
        return;
      }

      finalData = data;
      batchesProcessed = batchNumber;
      retryMappings = data.mappings || retryMappings;
      App.mappings = retryMappings;
      App.mappingWarning = data.mapping_warning || "";
      if (data.mapping_warning) lastWarning = data.mapping_warning;
      buildMappingTable();
      renderMappingWarning();

      const mapped = data.mapped_count ?? retryMappings.filter(m => m.api_id != null).length;
      const total = data.total_count ?? App.testCases.length;
      const newMapped = data.new_mapped_count ?? 0;
      if (status) {
        status.textContent = `Batch ${batchNumber}/${totalBatches} done; mapped ${mapped}/${total} (${newMapped} new).`;
      }

      if (data.paused) { if (status) status.textContent = "Mapping paused — click Retry Mapping to resume."; break; }
      if (data.complete || data.done) break;
      const nextStart = data.next_start_index ?? batchEnd;
      if (nextStart <= startIndex) break;
      startIndex = nextStart;
    }

    const mapped = finalData?.mapped_count ?? retryMappings.filter(m => m.api_id != null).length;
    const total = finalData?.total_count ?? App.testCases.length;
    const complete = finalData?.complete || mapped === total;
    App.mappingWarning = complete
      ? ""
      : `Retry mapping processed ${batchesProcessed} batch(es) of up to ${batchSize} captured requests and mapped ${mapped}/${total} test cases. Please map the remaining cases manually.`;
    if (!complete && lastWarning) {
      App.mappingWarning += ` Last LLM warning: ${lastWarning}`;
    }
    renderMappingWarning();
    if (status) {
      status.textContent = complete
        ? `Mapped ${mapped}/${total} using ${batchesProcessed} batch${batchesProcessed === 1 ? "" : "es"}.`
        : `Mapped ${mapped}/${total}; review remaining rows manually.`;
    }
  } catch (e) {
    alert("Retry mapping failed: " + e.message);
    if (status) status.textContent = "Retry failed";
  } finally {
    if (btn) btn.disabled = false;
    if (pauseBtn) pauseBtn.classList.add("hidden");
    if (spinner) spinner.classList.add("hidden");
  }
}

// ---------------------------------------------------------------
// Custom flow-select dropdown
// ---------------------------------------------------------------

function getFlowToTcMap() {
  const map = {};
  Object.entries(App.rowState).forEach(([tcNum, state]) => {
    if (state.flowId != null) map[state.flowId] = parseInt(tcNum, 10);
  });
  return map;
}

function setTriggerLabel(tcNum, flow) {
  const lbl = qs(`#flow-trigger-${tcNum} .flow-trigger-label`);
  if (!lbl) return;
  lbl.textContent = flow
    ? `${flow.id}. ${flow.method} ${flow.status} ${flow.path}`
    : "-- select --";
}

function closeAllFlowPanels() {
  qsa(".flow-panel.open").forEach(p => p.classList.remove("open"));
}

function openFlowPanel(tcNum, expectedStatus) {
  closeAllFlowPanels();
  const panel = qs(`#flow-panel-${tcNum}`);
  if (!panel) return;

  const flowToTc = getFlowToTcMap();

  // Build option elements
  const items = [`<div class="flow-opt" data-fid="" data-tc="${tcNum}"><span class="flow-opt-text">-- select --</span></div>`];
  App.capturedFlows.forEach(f => {
    const mappedTc = flowToTc[f.id];
    const isSelf  = mappedTc === tcNum;
    const isOther = mappedTc != null && !isSelf;
    const cls   = isSelf ? " opt-selected" : "";
    const badge = isSelf
      ? `<span class="tc-tag tc-tag-self">TC${tcNum}</span>`
      : isOther
      ? `<span class="tc-tag tc-tag-other">TC${mappedTc}</span>`
      : "";
    items.push(
      `<div class="flow-opt${cls}" data-fid="${f.id}" data-tc="${tcNum}">` +
      `<span class="flow-opt-text">${f.id}. ${escHtml(f.method)} ${f.status} ${escHtml(f.path)}</span>` +
      `${badge}</div>`
    );
  });
  panel.innerHTML = items.join("");
  panel.classList.add("open");

  // Scroll selected into view
  const sel = panel.querySelector(".opt-selected");
  if (sel) sel.scrollIntoView({ block: "nearest" });

  // Option click handlers
  qsa(".flow-opt", panel).forEach(opt => {
    opt.addEventListener("click", e => {
      e.stopPropagation();
      const fid = opt.dataset.fid ? parseInt(opt.dataset.fid, 10) : null;
      const tc  = parseInt(opt.dataset.tc, 10);
      selectFlow(tc, fid, expectedStatus);
    });
  });
}

function selectFlow(tcNum, fid, expectedStatus) {
  App.rowState[tcNum].flowId = fid;
  App.rowState[tcNum].manuallySelected = fid != null;
  const newFlow = fid != null ? App.capturedFlows.find(f => f.id === fid) || null : null;
  App.rowState[tcNum].correctedStatus = newFlow ? newFlow.status : expectedStatus;
  setTriggerLabel(tcNum, newFlow);
  refreshDetail(tcNum, newFlow);
  refreshStatusCell(tcNum, expectedStatus, newFlow);
  closeAllFlowPanels();
}

function buildMappingTable() {
  const tbody = qs("#mapping-tbody");
  tbody.innerHTML = "";

  const mappingByTc = {};
  (App.mappings || []).forEach(m => { mappingByTc[parseInt(m.tc, 10)] = m; });

  const defaultPreset = App.presets[App.activePresetKey] || [];

  App.testCases.forEach((tc, idx) => {
    const tcNum = parseInt(tc.testScenarioNumber || (idx + 1), 10);
    const previousState = App.rowState[tcNum] || {};
    const mapping = mappingByTc[tcNum] || {};
    const mappingHasApiId = Object.prototype.hasOwnProperty.call(mapping, "api_id");
    const rawMappedFlowId = mappingHasApiId
      ? mapping.api_id
      : (previousState.manuallySelected ? previousState.flowId : null);
    const previousFlowId = previousState.flowId == null ? null : parseInt(previousState.flowId, 10);
    const parsedMappedFlowId = rawMappedFlowId == null ? NaN : parseInt(rawMappedFlowId, 10);
    const mappedFlowId = Number.isNaN(parsedMappedFlowId) ? null : parsedMappedFlowId;
    const confidence = mapping.confidence ?? (mappedFlowId != null ? 100 : 0);
    const lowConf = confidence < 50;

    const matchedFlow = App.capturedFlows.find(f => f.id === mappedFlowId) || null;
    const capturedStatus = matchedFlow ? matchedFlow.status : null;
    const expectedStatus = parseInt(tc.expectedResponseCode || 200, 10);
    const correctedStatus = capturedStatus !== null
      ? capturedStatus
      : (previousState.correctedStatus || expectedStatus);

    App.rowState[tcNum] = {
      flowId: mappedFlowId,
      commonIgnore: previousState.commonIgnore ? [...previousState.commonIgnore] : [...defaultPreset],
      correctedStatus,
      testStresser: previousState.testStresser || "Y",
      manuallySelected: previousState.manuallySelected === true && previousFlowId === mappedFlowId,
      tcObjective: tc.testObjective || tc.testObjectivs || Object.values(tc)[1] || "",
    };

    let confClass = "conf-green";
    if (confidence < 50) confClass = "conf-red";
    else if (confidence < 75) confClass = "conf-yellow";

    let statusHtml = "";
    if (capturedStatus === null) {
      statusHtml = `<input type="number" class="status-input" data-tc="${tcNum}" value="${expectedStatus}">`;
    } else if (capturedStatus === expectedStatus) {
      statusHtml = `<span class="status-ok">${capturedStatus}</span>`;
    } else {
      statusHtml = `<span class="status-strike">${expectedStatus}</span>` +
        `<span class="status-arrow">→</span>` +
        `<span class="status-new">${capturedStatus}</span>`;
    }

    // ---- Main row ----
    const tr = document.createElement("tr");
    if (lowConf) tr.classList.add("row-warn");
    tr.dataset.tc = tcNum;

    tr.innerHTML = `
      <td style="white-space:nowrap">
        ${lowConf ? '<span class="warn-icon" title="Low confidence — pick manually">⚠</span> ' : ""}${tcNum}
      </td>
      <td>
        <div class="tc-objective">${escHtml(tc.testObjective || "")}</div>
        <div class="expand-toggle" data-tc="${tcNum}">▶ Show details</div>
      </td>
      <td>
        <div class="flow-select-wrap">
          <div class="flow-trigger" id="flow-trigger-${tcNum}">
            <span class="flow-trigger-label">-- select --</span>
            <span class="flow-trigger-arrow">▾</span>
          </div>
          <div class="flow-panel" id="flow-panel-${tcNum}"></div>
        </div>
      </td>
      <td><span class="conf-badge ${confClass}"${mapping.reason ? ` title="${escHtml(mapping.reason)}"` : ""}>${confidence}%</span></td>
      <td id="status-cell-${tcNum}">${statusHtml}</td>
      <td>
        <select class="select-input stresser-select" data-tc="${tcNum}" style="padding:5px 8px;font-size:.82rem;width:100%">
          <option value="Y" selected>Y</option>
          <option value="N">N</option>
        </select>
      </td>
    `;

    // ---- Detail row (full-width expansion) ----
    const detailTr = document.createElement("tr");
    detailTr.className = "detail-row";
    detailTr.style.display = "none";
    detailTr.innerHTML = `
      <td colspan="6" class="detail-cell">
        <div class="detail-inner">
          <div class="detail-col">
            <div class="detail-label">Request Body</div>
            <div class="json-block" id="req-${tcNum}">(no flow selected)</div>
            <div class="detail-label">Response Body</div>
            <div class="json-block" id="resp-${tcNum}">(no flow selected)</div>
          </div>
          <div class="detail-col">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <div class="detail-label" style="margin-bottom:0">commonIgnore</div>
              <select class="select-input ci-preset-select" data-tc="${tcNum}" style="padding:4px 8px;font-size:.76rem;width:auto;min-width:150px">
                <option value="standard_ignore_list">Standard Preset</option>
                <option value="no_commonignore">No CommonIgnore</option>
              </select>
            </div>
            <div class="ci-tags" id="ci-tags-${tcNum}"></div>
            <div class="ci-add-form">
              <input type="text" class="ci-add-input" id="ci-add-input-${tcNum}" placeholder="field name">
              <button class="ci-add-btn" data-tc="${tcNum}">+ Add</button>
            </div>
          </div>
        </div>
      </td>
    `;

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);

    if (matchedFlow) setTriggerLabel(tcNum, matchedFlow);
    refreshDetail(tcNum, matchedFlow);
    refreshCiTags(tcNum);

    tr.querySelector(".flow-trigger").addEventListener("click", e => {
      e.stopPropagation();
      openFlowPanel(tcNum, expectedStatus);
    });

    tr.addEventListener("change", e => {
      if (e.target.classList.contains("status-input")) {
        App.rowState[tcNum].correctedStatus = parseInt(e.target.value, 10);
      }
    });

    tr.querySelector(".stresser-select").addEventListener("change", e => {
      App.rowState[tcNum].testStresser = e.target.value;
    });

    tr.querySelector(".expand-toggle").addEventListener("click", () => {
      const isOpen = detailTr.style.display !== "none";
      detailTr.style.display = isOpen ? "none" : "table-row";
      tr.querySelector(".expand-toggle").textContent = isOpen ? "▶ Show details" : "▼ Hide details";
    });

    detailTr.querySelector(".ci-preset-select").addEventListener("change", e => {
      const preset = App.presets[e.target.value] || [];
      App.rowState[tcNum].commonIgnore = [...preset];
      refreshCiTags(tcNum);
    });

    detailTr.querySelector(".ci-add-btn").addEventListener("click", () => {
      const inp = qs(`#ci-add-input-${tcNum}`);
      const val = inp.value.trim();
      if (val && !App.rowState[tcNum].commonIgnore.includes(val)) {
        App.rowState[tcNum].commonIgnore.push(val);
        refreshCiTags(tcNum);
      }
      inp.value = "";
    });
  });
}

function refreshDetail(tcNum, flow) {
  const reqEl = qs(`#req-${tcNum}`);
  const respEl = qs(`#resp-${tcNum}`);
  if (!reqEl || !respEl) return;
  if (!flow) {
    reqEl.textContent = "(no flow selected)";
    respEl.textContent = "(no flow selected)";
    return;
  }
  reqEl.textContent = JSON.stringify(flow.request_body || {}, null, 2);
  respEl.textContent = JSON.stringify(flow.response_body || {}, null, 2);
}

function refreshStatusCell(tcNum, expectedStatus, flow) {
  const cell = qs(`#status-cell-${tcNum}`);
  if (!cell) return;
  const capturedStatus = flow ? flow.status : null;
  let html = "";
  if (capturedStatus === null) {
    html = `<input type="number" class="status-input" data-tc="${tcNum}" value="${App.rowState[tcNum].correctedStatus}">`;
  } else if (capturedStatus === expectedStatus) {
    html = `<span class="status-ok">${capturedStatus}</span>`;
  } else {
    html = `<span class="status-strike">${expectedStatus}</span><span class="status-arrow">→</span><span class="status-new">${capturedStatus}</span>`;
  }
  cell.innerHTML = html;
  // Re-attach listener for any new input
  const inp = cell.querySelector(".status-input");
  if (inp) {
    inp.addEventListener("change", () => {
      App.rowState[tcNum].correctedStatus = parseInt(inp.value, 10);
    });
  }
}

function refreshCiTags(tcNum) {
  const container = qs(`#ci-tags-${tcNum}`);
  if (!container) return;
  container.innerHTML = "";
  const tags = App.rowState[tcNum].commonIgnore || [];
  tags.forEach(field => {
    const tag = el("span", { class: "ci-tag" },
      field,
      el("span", {
        class: "ci-tag-remove",
        title: "Remove",
        onclick: () => {
          App.rowState[tcNum].commonIgnore = App.rowState[tcNum].commonIgnore.filter(f => f !== field);
          refreshCiTags(tcNum);
        },
      }, " ×")
    );
    container.appendChild(tag);
  });
}

// ---------------------------------------------------------------
// Confirm — send to server, generate artifacts
// ---------------------------------------------------------------
async function handleConfirm() {
  const apiName = qs("#api-name").value.trim();
  const module = qs("#module").value.trim();
  const manifest = qs("#manifest").value;
  const owner = qs("#owner").value.trim();
  const businessFunction = qs("#business-function").value.trim();

  const steps = App.testCases.map((tc, idx) => {
    const tcNum = parseInt(tc.testScenarioNumber || (idx + 1), 10);
    const state = App.rowState[tcNum] || {};

    let capturedApi;
    if (App.earlyMode) {
      const rawPath = state.mappedApi || "";
      capturedApi = {
        id: tcNum,
        method: state.method || "GET",
        path: rawPath,
        path_normalized: _normalizePath(rawPath),
        url_param: "NA",
        request_body: _safeParseJson(state.requestBody),
        response_body: _safeParseJson(state.responseBody),
        status: state.correctedStatus || 200,
      };
    } else {
      const flow = App.capturedFlows.find(f => f.id === state.flowId) || null;
      capturedApi = flow || {};
    }

    return {
      tc_number: tcNum,
      test_objective: tc.testObjective || "",
      captured_api: capturedApi,
      corrected_status: state.correctedStatus || parseInt(tc.expectedResponseCode || 200, 10),
      url_param: "NA",
      common_ignore: state.commonIgnore || [],
      test_stresser: state.testStresser || "Y",
    };
  });

  const payload = {
    api_name: apiName,
    module,
    manifest,
    owner,
    business_function: businessFunction,
    scenario: "y",
    steps,
    append_mode: App.appendMode || false,
    start_from_case: App.startFromCase || 1,
  };

  const btn = qs("#btn-confirm");
  btn.disabled = true;
  btn.textContent = "Generating...";

  try {
    const res = await fetch("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      buildStage4(data);
      goToStage(4);
    } else {
      alert("Generation failed: " + (data.error || "unknown error"));
      btn.disabled = false;
      btn.textContent = "Done — Generate Artifacts";
    }
  } catch (e) {
    alert("Error: " + e.message);
    btn.disabled = false;
    btn.textContent = "Done — Generate Artifacts";
  }
}

// ---------------------------------------------------------------
// Stage 4 — Complete
// ---------------------------------------------------------------
function buildStage4(data) {
  // ---- Stats ----
  const apiName       = qs("#api-name").value.trim();
  const module        = qs("#module").value.trim() || "—";
  const manifest      = qs("#manifest").value;
  const owner         = qs("#owner").value.trim() || "—";
  const businessFn    = qs("#business-function").value.trim() || "—";
  const totalCases    = App.testCases.length;
  const mappedCases   = App.earlyMode
    ? Object.values(App.rowState).filter(s => s.mappedApi && s.mappedApi.trim() !== "").length
    : Object.values(App.rowState).filter(s => s.flowId != null).length;
  const unmappedCases = totalCases - mappedCases;

  const statsCard = qs("#stats-card");
  statsCard.innerHTML = `
    <div class="stats-headline">
      <strong>${totalCases}</strong> test case${totalCases !== 1 ? "s" : ""} scripted for
      <strong>${escHtml(apiName)}</strong>
    </div>
    <div class="stats-strip">
      <div class="stat-chip">
        <div class="stat-val">${totalCases}</div>
        <div class="stat-label">Total Cases</div>
      </div>
      <div class="stat-chip">
        <div class="stat-val">${mappedCases}</div>
        <div class="stat-label">Mapped</div>
      </div>
      <div class="stat-chip ${unmappedCases > 0 ? "stat-chip-warn" : ""}">
        <div class="stat-val">${unmappedCases}</div>
        <div class="stat-label">Unmapped</div>
      </div>
      <div class="stat-chip">
        <div class="stat-val">${escHtml(manifest)}</div>
        <div class="stat-label">Manifest</div>
      </div>
      <div class="stat-chip">
        <div class="stat-val stat-val-md">${escHtml(module)}</div>
        <div class="stat-label">Module</div>
      </div>
      <div class="stat-chip">
        <div class="stat-val stat-val-md">${escHtml(owner)}</div>
        <div class="stat-label">Person</div>
      </div>
      <div class="stat-chip">
        <div class="stat-val stat-val-sm">${escHtml(businessFn)}</div>
        <div class="stat-label">Business Function</div>
      </div>
    </div>
  `;

  // ---- Files ----
  const fileList = qs("#file-list");
  fileList.innerHTML = "";
  const files = data.files || {};
  const labels = {
    api_inputs:    "apiInputs CSV",
    payload_json:  "Payload JSON",
    expected_json: "Expected JSON",
    manifest:      "Manifest CSV",
  };
  Object.entries(files).forEach(([key, path]) => {
    const filename = path.split("/").pop();
    const li = el("li", {},
      el("span", { class: "file-icon" }, "✓"),
      el("div", { class: "file-info" },
        el("span", { class: "file-label" }, labels[key] || key),
        el("code", { class: "file-path" }, filename)
      )
    );
    fileList.appendChild(li);
  });
}

// ---------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------
function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------
// Manifest option dropdowns (Module / Person / Business Function)
// ---------------------------------------------------------------
function populateDatalist(id, values) {
  const dl = qs(`#${id}`);
  if (!dl) return;
  dl.innerHTML = values.map(v => `<option value="${escHtml(v)}">`).join("");
}

async function loadManifestOptions() {
  try {
    const res = await fetch("/api/manifest-options");
    const data = await res.json();
    populateDatalist("module-options",            data.modules            || []);
    populateDatalist("person-options",           data.person           || []);
    populateDatalist("business-function-options", data.business_functions || []);
  } catch (e) {
    // non-fatal — user can still type values manually
  }
}

// ---------------------------------------------------------------
// Init
// ---------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  goToStage(1);
  initStage1();
  initStage2();

  // Close custom dropdowns when clicking outside
  document.addEventListener("click", () => closeAllFlowPanels());

  // Load presets and manifest options eagerly
  fetch("/api/presets").then(r => r.json()).then(p => { App.presets = p; }).catch(() => {});
  loadManifestOptions();
});
