/*
 * PII Exposure Scanner: the page's behaviour.
 *
 * Talks only to this server's /api (app.py) and shows what the pipeline
 * actually wrote: nothing here is computed, guessed or made up. Document
 * text is always inserted as text, never as HTML, so a document cannot
 * inject markup into the page.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------
  // Labels
  // ------------------------------------------------------------

  // The client-side upload, then the server's steps (src/web/jobs.py)
  const STEPS = [
    ["upload", "Upload file"],
    ["extract", "Read document"],
    ["check", "Check extraction"],
    ["scan", "Find personal data"],
    ["redact", "Make safe copy"],
    ["validate", "Check safety"],
    ["audit", "Review results"],
    ["file_redact", "Create redacted file"],
    ["file_check", "Check redacted file"],
  ];

  const STEP_ICONS = { pending: "", running: "", done: "✓", failed: "✕", skipped: "–" };
  const STEP_TEXT = { pending: "waiting", running: "in progress", done: "done", failed: "failed", skipped: "skipped" };

  // Every type the detectors report, in the order the summary lists them
  const TYPE_LABELS = {
    PERSON_NAME: ["People's names", "Person's name"],
    EMAIL: ["Email addresses", "Email address"],
    PHONE: ["Phone numbers", "Phone number"],
    EMPLOYEE_ID: ["Employee IDs", "Employee ID"],
    US_SSN: ["US Social Security numbers", "US Social Security number"],
    INDIAN_PAN: ["Indian PAN numbers", "Indian PAN number"],
    PASSPORT_NUMBER: ["Passport numbers", "Passport number"],
    NATIONAL_ID: ["National IDs", "National ID"],
    DATE_OF_BIRTH: ["Dates of birth", "Date of birth"],
    POSTAL_ADDRESS: ["Home addresses", "Home address"],
    PAYMENT_CARD: ["Payment card numbers", "Payment card number"],
    BANK_ACCOUNT: ["Bank account numbers", "Bank account number"],
    IP_ADDRESS: ["IP addresses", "IP address"],
    UNREADABLE_TEXT: ["Hard-to-read text", "Hard-to-read text"],
  };

  const VERDICTS = {
    PASS: { label: "Safe to share", cls: "pass", text: "No personal information is known to remain in the safe copy." },
    NEEDS_REVIEW: {
      label: "Needs review", cls: "review",
      text: "Nothing is known to remain, but something could not be confirmed. A person should review the issues below before sharing it.",
    },
    FAIL: { label: "Not safe to share", cls: "fail", text: "Personal information remains in the safe copy, or the copy does not match the original document." },
  };

  // The verdict once the review is counted: PASS when a reviewer answered
  // every issue and the release gate let the copy go (jobs.approval)
  function finalVerdict(job) {
    const result = job.result || {};
    return job.state === "completed" && result.final_verdict ? result.final_verdict : result.verdict;
  }

  function passedAfterReview(job) {
    return job.result.verdict === "NEEDS_REVIEW" && finalVerdict(job) === "PASS";
  }

  // What each validator check (src/pii/validator.py) confirms
  const CHECKS = {
    provenance: "The report and safe copy belong to this document",
    report: "Each flagged item matches the original text",
    replacements: "Every flagged item was replaced",
    ocr: "The read text is reliable and hidden where it was redacted",
    extraction: "The document was read completely",
    rescan: "A second scan of the safe copy finds no personal details",
    known_values: "No detected value is left anywhere in the copy",
    structure: "The structure stayed the same apart from the redaction tags",
  };

  const LOCATIONS = {
    header: "Header", footer: "Footer", text_box: "Text box", notes: "Speaker notes",
    footnote: "Footnote", endnote: "Endnote", comment: "Comment", template: "Slide template",
  };

  const CAPTIONS = {
    image_ocr: "Image text (OCR)",
    image: "Image",
    chart: "Chart",
    diagram: "Diagram",
    embedded_object: "Embedded object (not opened)",
  };

  const LOW_OCR_CONFIDENCE = 60;

  // The API this page was written for (API_VERSION in app.py)
  const API_VERSION = 3;

  const RESTART_SERVER = "The scanner server is running older code than this page. "
    + "Stop it (Ctrl+C in its terminal) and start it again with: python app.py";

  const SANITIZED_TAG = /\[REDACTED(?:: [A-Z0-9_]+)?\]/g;
  const TAGGED_TAG = /\[[A-Z0-9_]+: [^\]\n]*\]/g;

  const TABS = ["extracted", "findings", "tagged", "sanitized", "validation"];
  const DOCUMENT_TABS = { extracted: "original", tagged: "tagged", sanitized: "sanitized" };

  const INTROS = {
    extracted: "This is the document text in reading order. Sensitive items are highlighted; click one to see the detail.",
    tagged: "Each detected item is marked with its type. This version still contains the personal data and is for review only.",
    sanitized: "Sensitive details are replaced with placeholders. This is the safe version that can be shared with an AI system only after it passes the safety check.",
  };

  // ------------------------------------------------------------
  // State and elements
  // ------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

  // The two ways in: a document to extract, or a document already extracted
  const PATHS = {
    document: { zone: $("drop-document"), input: $("input-document"), label: "Document file" },
    extracted: { zone: $("drop-extracted"), input: $("input-extracted"), label: "Extracted JSON" },
  };

  const ui = {
    fileRow: $("file-row"),
    fileName: $("file-name"),
    fileMeta: $("file-meta"),
    scan: $("scan-button"),
    progress: $("progress"),
    progressBar: $("progress-bar"),
    status: $("status-line"),
    error: $("error-line"),
    after: $("after-actions"),
    newScan: $("new-scan"),
    deleteScan: $("delete-scan"),
    summary: $("findings-summary"),
    steps: $("steps"),
    decision: $("decision"),
    results: $("results"),
  };

  const state = {
    file: null,
    kind: null,
    maxBytes: 100 * 1024 * 1024,
    maxMb: 100,
    fileTypes: { document: [".pdf", ".pptx", ".docx"], extracted: [".json"] },
    busy: false,
    job: null,
    uploadStep: { status: "pending", detail: null },
    poll: null,
    pollFailures: 0,
    data: {},
    rendered: {},
    blockNodes: {},
    pageNodes: {},
    blockTexts: new Map(),
    findingRows: new Map(),
    activeTab: "extracted",
    // The tab to show again after a review answer, and the reviewer's name
    returnTab: null,
    reviewer: "",
  };

  // ------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------

  // An element with attributes, events and children; text is always text
  function el(tag, options, children) {
    const node = document.createElement(tag);

    for (const [key, value] of Object.entries(options || {})) {
      if (value === undefined || value === null || value === false) continue;

      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }

    append(node, children);

    return node;
  }

  function append(node, children) {
    for (const child of [children].flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }

    return node;
  }

  function fill(node, children) {
    node.replaceChildren();
    return append(node, children);
  }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ headers: { Accept: "application/json" } }, options));
    let body = null;

    try {
      body = await response.json();
    } catch (_) {
      body = null;
    }

    if (!response.ok) {
      const error = new Error((body && body.error) || `The request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }

    return body;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatConfidence(value) {
    return typeof value === "number" ? value.toFixed(2) : "";
  }

  function humanize(text) {
    const words = String(text || "").replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function typeLabel(type, singular) {
    const labels = TYPE_LABELS[type];
    return labels ? labels[singular ? 1 : 0] : humanize(type.toLowerCase());
  }

  function statusClass(status) {
    return { PASS: "pass", NEEDS_REVIEW: "review", FAIL: "fail" }[status] || "";
  }

  function statusLabel(status) {
    return String(status || "").replace(/_/g, " ");
  }

  function badge(text, cls) {
    return el("span", { class: `badge ${cls || ""}`, text });
  }

  function section(title, content) {
    return [el("h3", { class: "section-title", text: title }), content];
  }

  // ------------------------------------------------------------
  // Choosing a file
  // ------------------------------------------------------------

  function bindUpload() {
    for (const [kind, path] of Object.entries(PATHS)) {
      path.input.addEventListener("change", () => {
        if (path.input.files.length) chooseFile(path.input.files[0], kind);
        // The same file can be chosen again
        path.input.value = "";
      });

      for (const type of ["dragenter", "dragover"]) {
        path.zone.addEventListener(type, (event) => {
          event.preventDefault();
          if (!state.busy) path.zone.classList.add("dragging");
        });
      }

      for (const type of ["dragleave", "drop"]) {
        path.zone.addEventListener(type, (event) => {
          event.preventDefault();
          path.zone.classList.remove("dragging");
        });
      }

      path.zone.addEventListener("drop", (event) => {
        if (state.busy) return;
        const file = event.dataTransfer && event.dataTransfer.files[0];
        if (file) chooseFile(file, kind);
      });
    }

    // A file dropped beside the drop areas must not open in the browser
    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());
  }

  function fileProblem(file, kind, extension) {
    if (!state.fileTypes[kind].includes(extension)) {
      if (kind === "document" && state.fileTypes.extracted.includes(extension)) {
        return `${file.name} is a JSON file: choose it under Extracted JSON.`;
      }

      if (kind === "extracted" && state.fileTypes.document.includes(extension)) {
        return `${file.name} is a document: choose it under Document file.`;
      }

      return kind === "document"
        ? `${file.name} is not a PDF, PPTX or DOCX file.`
        : `${file.name} is not a JSON file.`;
    }

    if (file.size === 0) return `${file.name} is empty.`;
    if (file.size > state.maxBytes) return `${file.name} is larger than ${state.maxMb} MB.`;

    return null;
  }

  function chooseFile(file, kind) {
    if (state.busy) return;

    clearError();

    if (state.job) resetJob();

    const extension = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    const problem = fileProblem(file, kind, extension);

    markChosen(problem ? null : kind);

    if (problem) {
      state.file = null;
      state.kind = null;
      ui.fileRow.hidden = true;
      ui.status.textContent = "";
      showError(problem);
      return;
    }

    state.file = file;
    state.kind = kind;
    ui.fileName.textContent = file.name;
    ui.fileMeta.textContent = `${PATHS[kind].label} · ${extension.slice(1).toUpperCase()} · ${formatBytes(file.size)}`;
    ui.fileRow.hidden = false;
    ui.scan.disabled = false;
    ui.status.textContent = kind === "extracted"
      ? "Ready to scan. Extraction will be skipped: the file is checked against the common JSON format instead."
      : "Ready to scan.";
  }

  function markChosen(kind) {
    for (const [name, path] of Object.entries(PATHS)) path.zone.classList.toggle("chosen", name === kind);
  }

  // ------------------------------------------------------------
  // Scanning
  // ------------------------------------------------------------

  function startScan() {
    if (!state.file || state.busy) return;

    if (state.serverOutdated) {
      showError(RESTART_SERVER);
      return;
    }

    resetJob();
    clearError();
    setBusy(true);
    setUploadStep("running", "0%");
    showProgress(0);
    ui.status.textContent = `Uploading ${state.file.name}…`;

    const form = new FormData();
    form.append("file", state.file);

    // XMLHttpRequest, not fetch: it reports upload progress
    const request = new XMLHttpRequest();
    request.open("POST", "/api/jobs");
    request.responseType = "json";
    request.setRequestHeader("Accept", "application/json");

    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      showProgress(percent);
      setUploadStep("running", `${percent}%`);
    });

    request.addEventListener("load", () => {
      const body = request.response || {};

      if (request.status !== 202) {
        const message = body.error || `The upload failed (${request.status}).`;
        setUploadStep("failed", message);
        stop(message);
        return;
      }

      setUploadStep("done", `${formatBytes(state.file.size)}, stored on this computer`);
      showProgress(null);
      state.job = body;
      renderJob(body);
      pollJob();
    });

    request.addEventListener("error", () => {
      setUploadStep("failed", "The connection failed");
      stop("The upload did not reach the scanner. Is python app.py still running?");
    });

    request.send(form);
  }

  function pollJob() {
    clearTimeout(state.poll);

    state.poll = setTimeout(async () => {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(state.job.job_id)}`);
        state.pollFailures = 0;
        state.job = job;
        renderJob(job);

        if (job.state === "completed" || job.state === "failed") {
          await finish(job);
          return;
        }
      } catch (error) {
        if (error.status === 404) {
          stop(error.message);
          return;
        }

        state.pollFailures += 1;
        ui.status.textContent = "Lost contact with the scanner. Retrying…";

        if (state.pollFailures > 30) {
          stop("Lost contact with the scanner. Is python app.py still running?");
          return;
        }
      }

      pollJob();
    }, 800);
  }

  async function finish(job) {
    setBusy(false);
    showProgress(undefined);
    ui.scan.disabled = true;
    ui.after.hidden = false;

    if (job.state === "failed") {
      showError(`The scan stopped: ${job.error}`);
      ui.status.textContent = "The scan did not complete. What it produced is shown below.";
    } else if (job.input_kind === "extracted_json") {
      ui.status.textContent = `Scan complete: ${job.file_name} (extracted from ${job.source_file})`;
    } else {
      ui.status.textContent = `Scan complete: ${job.file_name}`;
    }

    await loadResults(job);
  }

  // An upload or connection that failed: the file can be scanned again
  function stop(message) {
    clearTimeout(state.poll);
    setBusy(false);
    showProgress(undefined);
    ui.status.textContent = "";
    showError(message);
  }

  function setBusy(busy) {
    state.busy = busy;

    for (const path of Object.values(PATHS)) {
      path.input.disabled = busy;
      path.zone.classList.toggle("disabled", busy);
    }

    ui.scan.disabled = busy || !state.file;
    ui.scan.textContent = busy ? "Scanning…" : "Scan Document";
    ui.after.hidden = busy || !state.job || !["completed", "failed"].includes(state.job.state);
  }

  function showProgress(value) {
    ui.progress.hidden = value === undefined;
    ui.progress.classList.toggle("indeterminate", value === null);
    ui.progressBar.style.width = typeof value === "number" ? `${value}%` : "";
  }

  function showError(message) {
    ui.error.textContent = message;
    ui.error.hidden = false;
  }

  function clearError() {
    // A server running older code stays reported until it is restarted
    if (state.serverOutdated) {
      showError(RESTART_SERVER);
      return;
    }

    ui.error.textContent = "";
    ui.error.hidden = true;
  }

  function setUploadStep(status, detail) {
    state.uploadStep = { status, detail };
    renderSteps(state.job);
  }

  // Forget the current job (its files stay until deleted)
  function resetJob() {
    clearTimeout(state.poll);
    state.job = null;
    state.uploadStep = { status: "pending", detail: null };
    state.data = {};
    state.rendered = {};
    state.blockNodes = {};
    state.pageNodes = {};
    state.blockTexts = new Map();
    state.findingRows = new Map();
    state.returnTab = null;
    ui.results.hidden = true;
    ui.after.hidden = true;
    for (const name of TABS) $(`view-${name}`).replaceChildren();
    renderSteps(null);
    renderSummary(null);
    renderDecision(null);
  }

  function reset() {
    resetJob();
    state.file = null;
    state.kind = null;
    markChosen(null);
    ui.fileRow.hidden = true;
    ui.status.textContent = "";
    clearError();
    showProgress(undefined);
    setBusy(false);
    PATHS.document.input.focus();
  }

  async function deleteScan() {
    if (!state.job) return;

    if (!window.confirm("Delete this scan's uploaded file and all of its outputs from this computer?")) return;

    try {
      await api(`/api/jobs/${encodeURIComponent(state.job.job_id)}`, { method: "DELETE" });
      reset();
      ui.status.textContent = "The scan's files were deleted from this computer.";
    } catch (error) {
      showError(error.message);
    }
  }

  // ------------------------------------------------------------
  // Progress: steps, findings summary, decision
  // ------------------------------------------------------------

  function renderJob(job) {
    renderSteps(job);
    renderSummary(job);
    renderDecision(job);

    if (job.state === "queued") {
      ui.status.textContent = "Waiting for the previous scan to finish…";
    } else if (job.state === "running") {
      const step = job.steps.find((item) => item.status === "running");
      ui.status.textContent = step ? `${step.label}…` : "Processing…";
    }
  }

  function renderSteps(job) {
    const server = new Map((job ? job.steps : []).map((step) => [step.name, step]));

    fill(ui.steps, STEPS.map(([name, label]) => {
      const step = name === "upload" ? state.uploadStep : server.get(name) || { status: "pending", detail: null };

      return el("li", { class: `step ${step.status}` }, [
        el("span", { class: "step-icon", "aria-hidden": "true", text: STEP_ICONS[step.status] }),
        el("span", null, [
          el("span", { class: "step-label", text: label }),
          el("span", { class: "visually-hidden", text: ` (${STEP_TEXT[step.status]})` }),
          step.detail ? el("span", { class: "step-detail", text: step.detail }) : null,
        ]),
      ]);
    }));
  }

  function zeroFindingsNote() {
    return el("p", {
      class: "note warn",
      text: "No personal information was found in this scan. That does not guarantee the file is fully clean, because OCR errors, unreadable images and unusual layouts can hide details. Please review the extraction warnings and the safety check results.",
    });
  }

  function renderSummary(job) {
    const result = job ? job.result : null;

    if (!result || result.findings_total === undefined) {
      let text = "Findings appear here after a document is scanned.";
      if (job && job.state === "failed") text = "No findings: the scan stopped before PII detection finished.";
      else if (job) text = "Waiting for PII detection to finish…";
      fill(ui.summary, el("p", { class: "muted", text }));
      return;
    }

    const total = result.findings_total;
    const counts = result.by_type || {};
    const largest = Math.max(1, ...Object.values(counts));
    const types = Object.keys(TYPE_LABELS).concat(Object.keys(counts).filter((type) => !TYPE_LABELS[type]));

    fill(ui.summary, [
      el("div", { class: "total" }, [
        el("span", { class: "total-number", text: total }),
        el("span", { class: "muted", text: total === 1 ? "finding" : "findings" }),
      ]),
      el("ul", { class: "breakdown", "aria-label": "Findings by type" }, types.map((type) => {
        const count = counts[type] || 0;
        const bar = el("span");
        bar.style.width = `${(count / largest) * 100}%`;

        return el("li", { class: count ? null : "zero" }, [
          el("span", { text: typeLabel(type) }),
          el("span", { class: "count", text: count }),
          el("span", { class: "bar", "aria-hidden": "true" }, bar),
        ]);
      })),
      result.names_detected === false
        ? el("p", { class: "note warn", text: "Names were not detected because the language model needed for this check is not available." })
        : null,
      total === 0 ? zeroFindingsNote() : null,
    ]);
  }

  function renderDecision(job) {
    const result = job ? job.result : {};

    if (!job || (!result.verdict && job.state !== "failed")) {
      ui.decision.replaceChildren();
      return;
    }

    const rows = [];

    if (result.verdict) {
      const verdict = finalVerdict(job);
      const info = VERDICTS[verdict];
      rows.push(el("div", { class: "decision-row" }, [
        el("span", { text: passedAfterReview(job) ? "Validation (after review)" : "Validation" }),
        badge(info ? info.label : verdict, info ? info.cls : ""),
      ]));
    }

    // Approved only once the job completed and the gate allowed it
    if (job.state === "completed" || job.state === "failed") {
      const approved = job.state === "completed" && result.approved === true;
      rows.push(el("div", { class: "decision-row" }, [
        el("span", { text: "AI processing" }),
        approved ? badge("APPROVED", "pass") : badge("BLOCKED", "fail"),
      ]));

      // Only once its steps ran (jobs.file_approval)
      if (result.file_verdict !== undefined) {
        const fileApproved = job.state === "completed" && result.file_approved === true;
        rows.push(el("div", { class: "decision-row" }, [
          el("span", { text: "Redacted file" }),
          fileApproved ? badge("READY", "pass") : result.file_verdict ? badge("BLOCKED", "fail") : badge("NOT MADE", "fail"),
        ]));
      }
    }

    fill(ui.decision, rows);
  }

  // ------------------------------------------------------------
  // Results
  // ------------------------------------------------------------

  async function loadResults(job) {
    const id = encodeURIComponent(job.job_id);
    const get = (path) => api(`/api/jobs/${id}/${path}`).catch((error) => ({ unavailable: error.message }));

    const [original, tagged, sanitized, findings, validation] = await Promise.all([
      get("document/original"),
      get("document/tagged"),
      get("document/sanitized"),
      get("findings"),
      get("validation"),
    ]);

    // The scan may have been reset while these loaded
    if (state.job !== job && (!state.job || state.job.job_id !== job.job_id)) return;

    state.data = { original, tagged, sanitized, findings, validation };
    state.blockTexts = indexTexts(original);
    state.rendered = {};
    ui.results.hidden = false;
    // After a review answer, back to the tab it was given on
    selectTab(state.returnTab || "extracted");
    state.returnTab = null;
  }

  function bindTabs() {
    TABS.forEach((name, index) => {
      const tab = $(`tab-${name}`);

      tab.addEventListener("click", () => selectTab(name));

      tab.addEventListener("keydown", (event) => {
        const moves = {
          ArrowRight: TABS[(index + 1) % TABS.length],
          ArrowLeft: TABS[(index - 1 + TABS.length) % TABS.length],
          Home: TABS[0],
          End: TABS[TABS.length - 1],
        };

        if (moves[event.key]) {
          event.preventDefault();
          selectTab(moves[event.key]);
          $(`tab-${moves[event.key]}`).focus();
        }
      });
    });
  }

  function selectTab(name) {
    state.activeTab = name;

    for (const other of TABS) {
      const selected = other === name;
      const tab = $(`tab-${other}`);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(`view-${other}`).hidden = !selected;
    }

    // Each tab is built the first time it is opened
    if (!state.rendered[name]) {
      const container = $(`view-${name}`);
      container.replaceChildren();

      if (name === "findings") renderFindingsTab(container);
      else if (name === "validation") renderValidationTab(container);
      else renderDocumentTab(container, name);

      state.rendered[name] = true;
    }
  }

  function unavailableNote(data, fallback) {
    return el("p", { class: "note warn", text: (data && data.unavailable) || fallback });
  }

  // The redacted file in the upload's own format has a check of its own (jobs.file_approval)
  function originalFileNote() {
    const result = state.job.result || {};
    const type = (result.file_name || "").split(".").pop().toUpperCase();

    if (state.job.state === "completed" && result.file_approved === true) {
      return el("p", {
        class: "note",
        text: `This redacted ${type} file passed its own safety check. You can download it from the Downloads section. The uploaded original still contains the personal information.`,
      });
    }

    return el("p", {
      class: result.file_verdict ? "note warn" : "note",
      text: result.file_verdict
        ? `This redacted file is not ready to download: ${result.file_reason || "it did not pass its own safety check."} The uploaded original still contains the personal information.`
        : "No redacted PDF, PPTX or DOCX file was created for this scan. The original upload still contains the personal information, including inside images.",
    });
  }

  function approvalNote() {
    const job = state.job;

    if (job.state === "completed" && job.result.approved === true) {
      return el("p", {
        class: "note",
        text: job.result.verdict === "NEEDS_REVIEW"
          ? "This safe copy is approved for AI use: a reviewer checked every issue before it was cleared."
          : "This safe copy is approved for AI use: it passed the safety check.",
      });
    }

    const verdict = job.result.verdict ? statusLabel(job.result.verdict) : "not validated";
    return el("p", {
      class: "note danger",
      text: `This copy is blocked from AI use (safety status: ${verdict}). Do not send it to an AI model.`,
    });
  }

  // ------------------------------------------------------------
  // Document text: extracted, tagged, sanitized
  // ------------------------------------------------------------

  function indexTexts(view) {
    const texts = new Map();

    if (!view || view.unavailable) return texts;

    for (const page of view.pages) {
      for (const block of page.blocks) {
        texts.set(block.block_id, block.text);
        for (const cell of block.cells || []) texts.set(cell.block_id, cell.text);
        if (block.author) texts.set(block.author.block_id, block.author.text);
      }
    }

    for (const property of view.properties) texts.set(property.block_id, property.text);

    return texts;
  }

  function findingsByBlock() {
    const byBlock = new Map();
    const data = state.data.findings;

    if (!data || data.unavailable) return byBlock;

    for (const item of data.findings) {
      if (!byBlock.has(item.block_id)) byBlock.set(item.block_id, []);
      byBlock.get(item.block_id).push(item);
    }

    return byBlock;
  }

  function renderDocumentTab(container, tab) {
    const view = state.data[DOCUMENT_TABS[tab]];

    append(container, el("p", { class: "tab-intro", text: INTROS[tab] }));

    if (tab === "sanitized") append(container, [approvalNote(), originalFileNote()]);

    if (!view || view.unavailable) {
      append(container, unavailableNote(view, "This text is not available for this scan."));
      return;
    }

    const marks = tab === "extracted" ? findingsByBlock() : null;
    const nodes = new Map();
    const pages = new Map();
    const fragment = document.createDocumentFragment();

    state.blockNodes[tab] = nodes;
    state.pageNodes[tab] = pages;

    for (const page of view.pages) {
      const blocks = el("div", { class: "doc-blocks" });

      for (const block of page.blocks) blocks.append(renderBlock(block, tab, marks, nodes));

      if (!page.blocks.length) blocks.append(el("p", { class: "muted", text: "No text was extracted from this page." }));

      const section = el("section", { class: "doc-page" }, [
        el("h3", { class: "doc-page-label" }, [
          page.label,
          page.hidden ? badge("Hidden slide") : null,
          page.low_text ? badge("Little or no text extracted", "review") : null,
        ]),
        blocks,
      ]);

      pages.set(page.page_number, section);
      fragment.append(section);
    }

    if (view.properties.length) {
      const list = el("ul", { class: "doc-properties" });

      for (const property of view.properties) {
        const item = el("li", { class: "doc-property" }, [
          el("span", { class: "name", text: humanize(property.name) }),
          el("span", null, richText(property.text, tab, marks && marks.get(property.block_id))),
        ]);
        nodes.set(property.block_id, item);
        list.append(item);
      }

      fragment.append(el("section", { class: "doc-page" }, [
        el("h3", { class: "doc-page-label", text: "File properties" }),
        list,
      ]));
    }

    container.append(fragment);
  }

  function blockCaption(block) {
    const parts = [];

    if (LOCATIONS[block.location]) parts.push(LOCATIONS[block.location]);
    if (CAPTIONS[block.block_type]) parts.push(CAPTIONS[block.block_type]);

    if (typeof block.ocr_confidence === "number" && block.ocr_confidence < LOW_OCR_CONFIDENCE) {
      parts.push(`low OCR confidence (${Math.round(block.ocr_confidence)})`);
    }

    return parts.join(" · ");
  }

  function renderBlock(block, tab, marks, nodes) {
    if (block.block_type === "table") return renderTable(block, tab, marks, nodes);

    const node = el("div", { class: `doc-block ${block.block_type}` });
    const caption = blockCaption(block);

    if (caption) {
      node.classList.add("framed");
      node.append(el("span", { class: "block-caption", text: caption }));
    }

    if (block.author) {
      const author = el("span", { class: "block-caption" }, [
        "Author: ",
        richText(block.author.text, tab, marks && marks.get(block.author.block_id)),
      ]);
      nodes.set(block.author.block_id, author);
      node.append(author);
    }

    if (typeof block.level === "number" && block.level > 0) node.style.marginLeft = `${block.level * 18}px`;

    append(node, richText(block.text, tab, marks && marks.get(block.block_id)));

    if (block.note) node.append(el("span", { class: "muted", text: block.note }));

    nodes.set(block.block_id, node);

    return node;
  }

  function renderTable(block, tab, marks, nodes) {
    const rows = new Map();

    for (const cell of block.cells) {
      if (!rows.has(cell.row)) rows.set(cell.row, []);
      rows.get(cell.row).push(cell);
    }

    const body = el("tbody");

    for (const row of [...rows.keys()].sort((a, b) => a - b)) {
      const tr = el("tr");

      for (const cell of rows.get(row).sort((a, b) => a.column - b.column)) {
        const td = el("td", {
          class: "doc-cell",
          rowspan: cell.row_span > 1 ? cell.row_span : null,
          colspan: cell.column_span > 1 ? cell.column_span : null,
        }, richText(cell.text, tab, marks && marks.get(cell.block_id)));

        nodes.set(cell.block_id, td);
        tr.append(td);
      }

      body.append(tr);
    }

    const caption = blockCaption(block);
    const wrap = el("div", { class: "doc-table-wrap" }, [
      caption ? el("span", { class: "block-caption", text: caption }) : null,
      el("table", { class: "doc-table" }, body),
    ]);

    nodes.set(block.block_id, wrap);

    return wrap;
  }

  function richText(text, tab, items) {
    if (tab === "extracted") return markFindings(text, items || [], true);
    if (tab === "tagged") return markTags(text, TAGGED_TAG, "tag tagged");
    return markTags(text, SANITIZED_TAG, "tag");
  }

  function markTags(text, pattern, cls) {
    const out = [];
    let position = 0;

    for (const match of text.matchAll(pattern)) {
      if (match.index > position) out.push(text.slice(position, match.index));
      out.push(el("span", { class: cls, text: match[0] }));
      position = match.index + match[0].length;
    }

    if (position < text.length) out.push(text.slice(position));

    return out;
  }

  // The text with each finding marked; overlapping findings share a mark
  function markFindings(text, items, interactive) {
    const spans = [];

    for (const item of [...items].sort((a, b) => a.start - b.start || b.end - a.end)) {
      const last = spans[spans.length - 1];

      if (last && item.start < last.end) {
        last.end = Math.max(last.end, item.end);
        last.items.push(item);
      } else {
        spans.push({ start: item.start, end: item.end, items: [item] });
      }
    }

    const out = [];
    let position = 0;

    for (const span of spans) {
      if (span.start < position || span.end > text.length) continue;

      if (span.start > position) out.push(text.slice(position, span.start));

      const first = span.items[0].finding_id;
      const title = span.items.map((item) => `${typeLabel(item.type, true)} (${item.finding_id})`).join(", ");
      const open = () => openFinding(first);

      out.push(el("mark", {
        class: "pii",
        title,
        dataset: { finding: first },
        tabindex: interactive ? "0" : null,
        onclick: interactive ? open : null,
        onkeydown: interactive ? (event) => { if (event.key === "Enter") open(); } : null,
      }, text.slice(span.start, span.end)));

      position = span.end;
    }

    if (position < text.length) out.push(text.slice(position));

    return out;
  }

  // ------------------------------------------------------------
  // PII findings
  // ------------------------------------------------------------

  function findingById(id) {
    const data = state.data.findings;
    return data && data.findings ? data.findings.find((item) => item.finding_id === id) : null;
  }

  function renderFindingsTab(container) {
    const data = state.data.findings;

    if (!data || data.unavailable) {
      append(container, unavailableNote(data, "The PII report is not available for this scan."));
      return;
    }

    if (data.status === "failed") {
      append(container, el("p", { class: "note danger", text: `The PII scan failed: ${data.error}` }));
      return;
    }

    const findings = data.findings;

    if (!findings.length) {
      append(container, zeroFindingsNote());
      return;
    }

    const counts = {};
    for (const item of findings) counts[item.type] = (counts[item.type] || 0) + 1;

    const search = el("input", {
      class: "input", type: "search", placeholder: "Search value, location or finding ID",
      "aria-label": "Search findings",
    });

    const typeFilter = el("select", { class: "input", "aria-label": "Filter by PII type" }, [
      el("option", { value: "", text: "All types" }),
      Object.keys(counts).sort().map((type) => el("option", { value: type, text: `${typeLabel(type)} (${counts[type]})` })),
    ]);

    const count = el("span", { class: "count", "aria-live": "polite" });
    const body = el("tbody");

    const rows = findings.map((item) => {
      const row = el("tr", {
        class: "finding-row",
        tabindex: "0",
        dataset: { finding: item.finding_id },
        onclick: () => selectFinding(item.finding_id),
        onkeydown: (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectFinding(item.finding_id);
          }
        },
      }, [
        el("td", { class: "nowrap", text: typeLabel(item.type, true) }),
        el("td", { class: "value", text: item.value }),
        el("td", { class: "num", text: formatConfidence(item.confidence) }),
        el("td", { class: "nowrap", text: item.page_label }),
        el("td", { text: item.location }),
        el("td", { class: "nowrap", text: humanize(item.review_status) }),
      ]);

      row.searchText = [item.value, item.location, item.finding_id, item.type, item.block_id].join(" ").toLowerCase();
      row.findingType = item.type;
      body.append(row);

      return row;
    });

    state.findingRows = new Map(rows.map((row) => [row.dataset.finding, row]));

    const apply = () => {
      const query = search.value.trim().toLowerCase();
      const type = typeFilter.value;
      let shown = 0;

      for (const row of rows) {
        const visible = (!type || row.findingType === type) && (!query || row.searchText.includes(query));
        row.hidden = !visible;
        shown += visible ? 1 : 0;
      }

      count.textContent = `Showing ${shown} of ${rows.length}`;
    };

    search.addEventListener("input", apply);
    typeFilter.addEventListener("change", apply);

    state.clearFindingFilters = () => {
      search.value = "";
      typeFilter.value = "";
      apply();
    };

    append(container, [
      el("div", { class: "toolbar" }, [search, typeFilter, count]),
      el("div", { class: "table-wrap" }, el("table", { class: "data-table" }, [
        el("thead", null, el("tr", null, ["Type", "Value", "Confidence", "Page / slide", "Location", "Review status"]
          .map((heading) => el("th", { scope: "col", text: heading })))),
        body,
      ])),
      el("div", { class: "inspector", id: "inspector" },
        el("p", { class: "muted", text: "Select a finding to see its surrounding text and source location." })),
    ]);

    apply();
  }

  function selectFinding(id) {
    const item = findingById(id);
    const inspector = $("inspector");

    if (!item || !inspector) return;

    for (const [rowId, row] of state.findingRows) row.classList.toggle("selected", rowId === id);

    const details = [
      ["Value", el("span", { class: "value", text: item.value })],
      ["Type", typeLabel(item.type, true)],
      ["Confidence", `${formatConfidence(item.confidence)} (a rule's ranking, not a probability)`],
      ["Location", item.location],
      ["Detector", `${item.detector || ""}${item.rule_id ? ` · ${item.rule_id}` : ""}`],
      ["Found because of", item.context_trigger],
      ["Action", item.action],
      ["Validation", item.validation],
      ["Validation issues", item.validation_issues && item.validation_issues.length ? item.validation_issues.join("; ") : null],
      ["Review status", humanize(item.review_status)],
      ["Image OCR confidence", typeof item.ocr_confidence === "number" ? Math.round(item.ocr_confidence) : null],
      ["Block", `${item.block_id} · characters ${item.start}–${item.end}`],
    ].filter(([, value]) => value !== null && value !== undefined && value !== "");

    fill(inspector, [
      el("h3", { text: `${typeLabel(item.type, true)} · ${item.finding_id}` }),
      el("dl", { class: "details" }, details.map(([term, value]) => [el("dt", { text: term }), el("dd", null, value)])),
      el("p", { class: "block-caption", text: "Surrounding text" }),
      contextBox(item),
      el("div", { class: "downloads" }, el("button", {
        type: "button", class: "button", text: "Show in extracted text", onclick: () => showInDocument(item),
      })),
    ]);
    // The inspector is filled on demand; keep it in view
    inspector.scrollIntoView({ block: "nearest" });
  }

  function contextBox(item) {
    const text = state.blockTexts.get(item.block_id);

    if (text === undefined) return el("p", { class: "muted", text: "The surrounding text is not available." });

    const around = 300;
    let start = 0;
    let end = text.length;

    if (text.length > around * 2 + (item.end - item.start)) {
      start = Math.max(0, item.start - around);
      end = Math.min(text.length, item.end + around);
    }

    const shifted = Object.assign({}, item, { start: item.start - start, end: item.end - start });

    return el("div", { class: "context-box" }, [
      start > 0 ? "…" : null,
      markFindings(text.slice(start, end), [shifted], false),
      end < text.length ? "…" : null,
    ]);
  }

  function showInDocument(item) {
    showBlock(item.block_id, item.page_number, item.finding_id);
  }

  // Whether the extracted text shows this block, or at least its page
  function inDocument(blockId, pageNumber) {
    const original = state.data.original;

    return state.blockTexts.has(blockId)
      || Boolean(original && original.pages && original.pages.some((page) => page.page_number === pageNumber));
  }

  // Open the extracted text at a block, or at its page when the block is not shown
  function showBlock(blockId, pageNumber, findingId) {
    selectTab("extracted");

    const block = state.blockNodes.extracted && state.blockNodes.extracted.get(blockId);
    const node = block || (state.pageNodes.extracted && state.pageNodes.extracted.get(pageNumber));
    if (!node) return;

    node.scrollIntoView({ block: block ? "center" : "start" });
    node.classList.remove("flash");
    void node.offsetWidth;
    node.classList.add("flash");

    for (const mark of document.querySelectorAll("#view-extracted mark.pii.selected")) mark.classList.remove("selected");

    if (!block || !findingId) return;

    for (const mark of block.querySelectorAll("mark.pii")) {
      if (mark.dataset.finding === findingId) mark.classList.add("selected");
    }
  }

  function openFinding(id) {
    selectTab("findings");

    const row = state.findingRows.get(id);
    if (!row) return;

    if (row.hidden && state.clearFindingFilters) state.clearFindingFilters();

    selectFinding(id);
    row.scrollIntoView({ block: "nearest" });
    row.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------
  // Validation and audit
  // ------------------------------------------------------------

  function verdictBox(verdict) {
    const job = state.job;
    const info = VERDICTS[verdict];
    const approved = job.state === "completed" && job.result.approved === true;
    const reviewed = approved && passedAfterReview(job);

    if (!info) {
      return el("div", { class: "verdict-box fail" }, [
        badge("NOT VALIDATED", "fail"),
        el("div", { class: "verdict-text" }, [
          el("strong", { text: "Blocked for AI processing" }),
          "Validation did not run, so nothing confirms the sanitized copy is free of PII.",
        ]),
      ]);
    }

    return el("div", { class: `verdict-box ${info.cls}` }, [
      badge(info.label, info.cls),
      el("div", { class: "verdict-text" }, [
        el("strong", { text: approved ? "Approved for AI processing" : "Blocked for AI processing" }),
        reviewed ? "Validation raised issues that needed review, and a reviewer checked every one (below)." : info.text,
        !approved && job.result.reason && verdict === "PASS" ? ` (${job.result.reason})` : null,
      ]),
    ]);
  }

  // readOnly: no answers of its own (an OCR issue, answered with the others)
  function issueItem(issue, readOnly) {
    const where = [];

    if (issue.finding_id) {
      where.push(el("button", {
        type: "button", class: "link-button", text: issue.finding_id,
        onclick: () => openFinding(issue.finding_id),
      }));
    }

    const place = [
      issue.block_id ? `block ${issue.block_id}` : null,
      issue.page_number ? `page ${issue.page_number}` : null,
    ].filter(Boolean).join(" · ");

    // The block (or page) opens in the extracted text, where the reviewer can read it
    if (place) {
      where.push(" ", inDocument(issue.block_id, issue.page_number)
        ? el("button", {
          type: "button", class: "link-button", text: place, title: "Show in extracted text",
          onclick: () => showBlock(issue.block_id, issue.page_number),
        })
        : place);
    }

    const item = el("li");
    const signed = issue.signed_off;

    // An issue that needs review: its badge opens the reviewer's choices
    const status = readOnly
      ? badge(signed ? "CHECKED" : statusLabel(issue.severity), signed ? "pass" : statusClass(issue.severity))
      : issue.severity === "NEEDS_REVIEW" && issue.key
      ? el("button", {
        type: "button",
        class: `badge badge-button ${signed ? "pass" : "review"}`,
        text: signed ? "CHECKED" : statusLabel(issue.severity),
        title: signed ? "Checked by a reviewer: select to see who, or to undo" : "Select to review this issue",
        "aria-expanded": "false",
        onclick: () => toggleReview(item, issue, status),
      })
      : badge(statusLabel(issue.severity), statusClass(issue.severity));

    return append(item, [
      status,
      " ",
      issue.reason,
      issue.occurrences ? ` (${issue.occurrences}×)` : null,
      where.length ? el("span", { class: "muted" }, [" — ", where]) : null,
    ]);
  }

  function toggleReview(item, issue, button) {
    const open = item.querySelector(".review-box");

    if (open) {
      open.remove();
      button.setAttribute("aria-expanded", "false");
      return;
    }

    button.setAttribute("aria-expanded", "true");
    item.append(reviewBox(issue));
  }

  // The reviewer's choices for one issue (src/pii/review.py)
  function reviewBox(issue) {
    const message = el("p", { class: "review-message", role: "alert", hidden: true });
    const fail = (text) => {
      message.textContent = text;
      message.hidden = false;
    };

    if (issue.signed_off) {
      return el("div", { class: "review-box" }, [
        el("span", { class: "muted", text: `Checked by ${issue.signed_off.reviewer} · ${formatTime(issue.signed_off.signed_at)}` }),
        el("button", {
          type: "button", class: "button subtle", text: "Undo",
          onclick: () => sendReview(`sign-offs/${encodeURIComponent(issue.key)}`, "DELETE", null, fail),
        }),
        message,
      ]);
    }

    const name = el("input", {
      class: "input", type: "text", placeholder: "Your name",
      maxlength: "80", value: state.reviewer || "",
    });

    const send = (path) => {
      state.reviewer = name.value.trim();

      if (!state.reviewer) {
        fail("Enter your name: it is recorded with your review.");
        name.focus();
        return;
      }

      sendReview(path, "POST", { key: issue.key, reviewer: state.reviewer }, fail);
    };

    return el("div", { class: "review-box" }, [
      // Who checked the issue: recorded with the answer, not a value to hide
      el("label", { class: "review-name" }, ["Reviewer", name]),
      issue.can_hide ? el("button", { type: "button", class: "button", text: "Hide it", onclick: () => send("hide") }) : null,
      el("button", { type: "button", class: "button", text: "Checked, it's fine", onclick: () => send("sign-offs") }),
      issue.can_hide
        ? el("p", { class: "muted review-hint", text: "Hide it redacts this text and runs the checks again. \"Checked\" answers on issues it does not change are kept." })
        : null,
      message,
    ]);
  }

  // Send a review answer; the job runs again and the page follows it
  async function sendReview(path, method, body, fail) {
    if (!state.job) return;

    if (state.busy) {
      fail("The checks are still running from your last answer: try again when they finish.");
      return;
    }

    try {
      const job = await api(`/api/jobs/${encodeURIComponent(state.job.job_id)}/review/${path}`, {
        method,
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });

      state.job = job;
      state.returnTab = state.activeTab;
      clearError();
      setBusy(true);
      renderJob(job);
      pollJob();
    } catch (error) {
      fail(error.message);
    }
  }

  function formatTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value || "") : date.toLocaleString();
  }

  // How far the review has got, "Hide all OCR issues", and the blocks a
  // reviewer hid text in (views.review_view)
  function reviewPanel(review, ocrIssues) {
    const hidden = review.hidden || [];

    const progress = el("p", {
      class: review.open ? "note warn" : "note",
      text: `${hidden.length} block${hidden.length === 1 ? "" : "s"} hidden · ${review.checked} checked · ${review.open} left to review. `
        + (review.open ? "Validation turns PASS when none are left." : "Nothing is left to review."),
    });

    return [
      progress,
      ocrIssues.length ? ocrGroup(ocrIssues) : null,
      hidden.length ? el("ul", { class: "plain-list" }, hidden.map(hiddenItem)) : null,
    ];
  }

  // Every issue about badly read OCR text, as one line with one answer for
  // all of them (review.py: hidden_all_report, sign_off_all, withdraw_all).
  // Each place is listed, read-only, under a fold.
  function ocrGroup(issues) {
    const job = state.job;
    const open = issues.filter((issue) => !issue.signed_off);
    const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
    const pages = [...new Set(issues.map((issue) => issue.page_number).filter((page) => page !== undefined && page !== null))]
      .sort((a, b) => a - b);
    const where = pages.length
      ? ` (page${pages.length === 1 ? "" : "s"} ${pages.slice(0, 8).join(", ")}${pages.length > 8 ? " …" : ""})`
      : "";

    const message = el("p", { class: "review-message", role: "alert", hidden: true });
    const fail = (text) => {
      message.textContent = text;
      message.hidden = false;
    };

    const name = el("input", {
      class: "input", type: "text", placeholder: "Your name",
      maxlength: "80", value: state.reviewer || "",
    });

    // Both answers record who gave them, and ask once more before applying
    const answer = (path, question) => () => {
      state.reviewer = name.value.trim();

      if (!state.reviewer) {
        fail("Enter your name: it is recorded with your review.");
        name.focus();
        return;
      }

      if (!window.confirm(question)) return;

      sendReview(path, "POST", { reviewer: state.reviewer }, fail);
    };

    let answers = null;

    if (!open.length) {
      const signed = issues[0].signed_off;
      answers = el("div", { class: "review-box" }, [
        el("span", { class: "muted", text: `All checked by ${signed.reviewer} · ${formatTime(signed.signed_at)}` }),
        el("button", {
          type: "button", class: "button subtle", text: "Undo",
          onclick: () => sendReview("sign-off-all", "DELETE", null, fail),
        }),
        message,
      ]);
    } else if (job.state === "completed") {
      answers = el("div", { class: "review-box" }, [
        el("label", { class: "review-name" }, ["Reviewer", name]),
        el("button", {
          type: "button", class: "button", text: "Hide all",
          onclick: answer("hide-all", `Hide the text of all ${plural(open.length, "place")}? It is redacted from the sanitized copy, so the AI will not see it.`),
        }),
        el("button", {
          type: "button", class: "button", text: "Checked, all fine",
          onclick: answer("sign-off-all", `Approve all ${plural(open.length, "place")} as they are? Their text goes to the AI unread, including any PII the OCR misread.`),
        }),
        el("p", {
          class: "muted review-hint",
          text: (open.length < issues.length ? `${issues.length - open.length} already checked: the buttons answer the other ${open.length}. ` : "")
            + "Hide all removes their text from the sanitized copy. Checked, all fine sends it to the AI as it is.",
        }),
        message,
      ]);
    }

    return el("div", { class: "ocr-group" }, [
      el("p", { class: "ocr-group-line" }, [
        open.length ? badge("NEEDS REVIEW", "review") : badge("CHECKED", "pass"),
        " ",
        `${plural(issues.length, "place")} where OCR text was read with low confidence${where}`,
      ]),
      answers,
      el("details", null, [
        el("summary", { text: `Show the ${plural(issues.length, "place")}` }),
        el("ul", null, issues.map((issue) => issueItem(issue, true))),
      ]),
    ]);
  }

  // One block a reviewer hid text in: block ID, types, who and when; never the text
  function hiddenItem(entry) {
    const types = entry.types.map((type) => (TYPE_LABELS[type] ? TYPE_LABELS[type][1] : humanize(type))).join(", ");
    const place = [`block ${entry.block_id}`, entry.page_number ? `page ${entry.page_number}` : null].filter(Boolean).join(" · ");

    return el("li", null, [
      badge("HIDDEN", "pass"),
      " ",
      types,
      el("span", { class: "muted" }, [
        " — ",
        inDocument(entry.block_id, entry.page_number)
          ? el("button", {
            type: "button", class: "link-button", text: place, title: "Show in extracted text",
            onclick: () => showBlock(entry.block_id, entry.page_number),
          })
          : place,
        entry.added_by ? ` · by ${entry.added_by} · ${formatTime(entry.added_at)}` : null,
      ]),
    ]);
  }

  function checkList(checks) {
    return el("div", { class: "check-list" }, checks.map((check) => {
      // Badly read OCR text is answered all at once, under Review (ocrGroup)
      const grouped = (check.issues || []).filter((issue) => issue.unreadable);
      const issues = (check.issues || []).filter((issue) => !issue.unreadable);
      const more = check.issues_total - issues.length - grouped.length;
      // PASS once a reviewer checked every issue (views.reviewed_validation)
      const status = check.final_status || check.status;
      const count = `${check.issues_total} issue${check.issues_total === 1 ? "" : "s"}`;

      return el("details", { class: "check", open: check.status !== "PASS" }, [
        el("summary", null, [
          el("span", { class: "check-status" }, badge(statusLabel(status), statusClass(status))),
          el("span", { class: "check-name" }, [CHECKS[check.name] || humanize(check.name), el("small", { text: check.name })]),
          el("span", { class: "muted nowrap", text: status !== check.status ? `${count}, all checked` : count }),
        ]),
        el("div", { class: "check-body" }, [
          check.notes.length ? el("ul", { class: "muted" }, check.notes.map((note) => el("li", { text: note }))) : null,
          grouped.length
            ? el("p", { class: "muted", text: `${grouped.length} place${grouped.length === 1 ? "" : "s"} where OCR text was read with low confidence: answered all together under Review, above.` })
            : null,
          issues.length
            ? el("ul", null, issues.map((issue) => issueItem(issue)))
            : grouped.length ? null : el("p", { class: "muted", text: "No issues." }),
          more > 0 ? el("p", { class: "muted", text: `…and ${more} more in the validation report.` }) : null,
        ]),
      ]);
    }));
  }

  function redactionBlock(redaction) {
    const summary = redaction.summary || {};
    const items = [
      ["Status", humanize(redaction.status)],
      ["Findings", summary.findings],
      ["Redacted", summary.redacted],
      ["Not redacted (reviewed as false positives)", summary.skipped],
      ["Failed", summary.failed],
    ].filter(([, value]) => value !== undefined && value !== null);

    const problems = (redaction.errors || []).map((error) => el("li", null, [
      badge("ERROR", "fail"), " ", error.reason, el("span", { class: "muted", text: ` — ${[error.finding_id, error.block_id].filter(Boolean).join(", ")}` }),
    ])).concat((redaction.warnings || []).map((warning) => el("li", null, [
      badge("WARNING", "review"), " ", warning.reason, el("span", { class: "muted", text: ` — ${warning.block_id || ""}` }),
    ])));

    return [
      el("dl", { class: "details" }, items.map(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })])),
      problems.length ? el("ul", { class: "plain-list" }, problems) : null,
    ];
  }

  function downloadsBlock() {
    const job = state.job;
    const id = encodeURIComponent(job.job_id);
    const data = state.data;
    const approved = job.state === "completed" && job.result.approved === true;
    const available = (value) => value && !value.unavailable;
    const link = (name, label) => el("a", { class: "button", href: `/api/jobs/${id}/download/${name}`, download: "", text: label });

    const validation = data.validation || {};

    // Served only if its release gate lets it go (approved_file)
    const fileApproved = job.state === "completed" && job.result.file_approved === true;
    const fileType = (job.result.file_name || "").split(".").pop().toUpperCase();

    const links = [
      fileApproved ? link("redacted", `Redacted ${fileType}`) : null,
      available(data.original) ? link("extracted", "Extracted JSON") : null,
      available(data.findings) ? link("findings", "Findings JSON") : null,
      available(data.sanitized) ? link("sanitized", approved ? "Sanitized JSON" : "Sanitized JSON (not approved)") : null,
      validation.validation ? link("validation", "Validation report") : null,
      validation.exposure ? link("audit-md", "Audit report (Markdown)") : null,
      validation.exposure ? link("audit-json", "Audit report (JSON)") : null,
    ].filter(Boolean);

    if (!links.length) return el("p", { class: "muted", text: "No files are available for this scan." });

    return [
      el("div", { class: "downloads" }, links),
      !fileApproved && job.result.file_reason
        ? el("p", { class: "note warn", text: `Redacted file not available: ${job.result.file_reason}` })
        : null,
      el("p", {
        class: "note",
        text: "The extracted JSON and the findings JSON hold the PII: handle them as sensitive. Choose the extracted JSON under Extracted JSON to scan the document again without extracting it. The validation and audit reports hold no PII values.",
      }),
    ];
  }

  function renderValidationTab(container) {
    const job = state.job;
    const data = state.data.validation;
    const verdict = data && data.validation ? finalVerdict(job) || data.validation.verdict : null;

    append(container, [verdictBox(verdict), originalFileNote()]);

    if (job.result.findings_total === 0) append(container, zeroFindingsNote());

    const errors = [job.error, data && data.report_error].filter(Boolean);

    if (errors.length) {
      append(container, section("Processing errors",
        el("ul", { class: "plain-list" }, [...new Set(errors)].map((error) => el("li", { text: error })))));
    }

    if (!data || data.unavailable) {
      append(container, [unavailableNote(data, "The validation results are not available."), section("Downloads", downloadsBlock())]);
      return;
    }

    const review = data.review || {};
    const ocrIssues = data.validation
      ? data.validation.checks.flatMap((check) => (check.issues || []).filter((issue) => issue.unreadable))
      : [];

    if (data.validation && (review.needing_review || (review.hidden || []).length || ocrIssues.length)) {
      append(container, section("Review", reviewPanel(review, ocrIssues)));
    }

    if (data.validation) append(container, section("Validation checks", checkList(data.validation.checks)));

    const warnings = data.extraction_warnings || [];

    append(container, section("Extraction warnings",
      warnings.length
        ? el("ul", { class: "plain-list" }, warnings.map((warning) => el("li", { text: warning })))
        : el("p", { class: "muted", text: "None reported." })));

    if (data.redaction) append(container, section("Redaction", redactionBlock(data.redaction)));

    append(container, section("Downloads", downloadsBlock()));
  }

  // ------------------------------------------------------------
  // Start
  // ------------------------------------------------------------

  async function init() {
    renderSteps(null);
    renderSummary(null);
    bindUpload();
    bindTabs();

    ui.scan.addEventListener("click", startScan);
    ui.newScan.addEventListener("click", reset);
    ui.deleteScan.addEventListener("click", deleteScan);

    // Nothing may fail silently: an unexpected error is shown, not just logged
    window.addEventListener("error", (event) => unexpected(event.message));
    window.addEventListener("unhandledrejection", (event) => unexpected(event.reason && event.reason.message));

    let config;

    try {
      config = await api("/api/config");
    } catch (_) {
      showError("Cannot reach the scanner. Start it with: python app.py");
      return;
    }

    state.maxMb = config.max_upload_mb || state.maxMb;
    state.maxBytes = state.maxMb * 1024 * 1024;
    state.fileTypes = {
      document: config.document_types || state.fileTypes.document,
      extracted: config.extracted_types || state.fileTypes.extracted,
    };
    $("max-size").textContent = state.maxMb;

    if (config.api_version !== API_VERSION) {
      state.serverOutdated = true;
      showError(RESTART_SERVER);
    }
  }

  function unexpected(message) {
    showError(
      `Something went wrong in this page${message ? `: ${message}` : ""}. `
      + "Reload the page; if it happens again, restart the server (python app.py)."
    );
  }

  init();
})();
