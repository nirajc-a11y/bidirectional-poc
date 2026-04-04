// --- State ---
let ws = null;
let callTimer = null;
let callStartTime = null;

// --- DOM Elements ---
const csvUpload = document.getElementById("csvUpload");
const startBtn = document.getElementById("startBtn");
const pauseBtn = document.getElementById("pauseBtn");
const stopBtn = document.getElementById("stopBtn");
const downloadBtn = document.getElementById("downloadBtn");
const statusBar = document.getElementById("statusBar");
const queueList = document.getElementById("queueList");
const progressContainer = document.getElementById("progressContainer");
const progressBar = document.getElementById("progressBar");
const progressText = document.getElementById("progressText");
const callInfo = document.getElementById("callInfo");
const transcriptFeed = document.getElementById("transcriptFeed");
const resultsList = document.getElementById("resultsList");

// --- WebSocket ---
function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${location.host}/ws`);

  ws.onopen = () => {
    console.log("WebSocket connected");
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    handleEvent(data);
  };

  ws.onclose = () => {
    console.log("WebSocket disconnected, reconnecting...");
    setTimeout(connectWebSocket, 2000);
  };
}

function handleEvent(event) {
  switch (event.type) {
    case "csv_loaded":
      renderQueue(event.rows);
      startBtn.disabled = false;
      downloadBtn.disabled = false;
      progressContainer.style.display = "block";
      updateProgress(event.rows);
      setStatus(`CSV loaded: ${event.count} claims`);
      break;

    case "status":
      setStatus(event.message);
      if (event.stats) updateStats(event.stats);
      if (event.message.includes("finished") || event.message.includes("completed")) {
        startBtn.disabled = false;
        pauseBtn.disabled = true;
        stopBtn.disabled = true;
        stopCallTimer();
        callInfo.style.display = "none";
        transcriptFeed.innerHTML = '<p class="empty-state">No active call</p>';
      }
      break;

    case "call_started":
      setStatus(`Calling: ${event.claim_number}`);
      showLiveCall(event.claim_data);
      updateQueueItemStatus(event.claim_number, "in-progress");
      startCallTimer();
      break;

    case "call_active":
      // Heartbeat - call still running
      break;

    case "call_completed":
      updateQueueItemStatus(event.claim_number, "completed");
      addResult(event.claim_number, event.results);
      if (event.stats) updateStats(event.stats);
      stopCallTimer();
      refreshQueue();
      break;

    case "call_failed":
      updateQueueItemStatus(event.claim_number, "failed");
      setStatus(`Call failed: ${event.claim_number} - ${event.reason}`);
      stopCallTimer();
      refreshQueue();
      break;

    case "call_no_answer":
      updateQueueItemStatus(event.claim_number, "no-answer");
      setStatus(`No answer: ${event.claim_number}`);
      if (event.stats) updateStats(event.stats);
      stopCallTimer();
      refreshQueue();
      break;

    case "transcript_line":
      addTranscriptLine(event.speaker, event.text);
      break;
  }
}

// --- UI Functions ---

function setStatus(message) {
  statusBar.textContent = message;
}

function renderQueue(rows) {
  queueList.innerHTML = "";
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "queue-item";
    item.id = `queue-${row.claim_number}`;
    const status = row.call_status || "pending";
    item.innerHTML = `
      <div>
        <div class="claim-id">${row.claim_number}</div>
        <div class="patient-name">${row.patient_name}</div>
      </div>
      <span class="badge badge-${status}">${status}</span>
    `;
    queueList.appendChild(item);
  });
  updateProgress(rows);
}

function updateQueueItemStatus(claimNumber, status) {
  const item = document.getElementById(`queue-${claimNumber}`);
  if (item) {
    const badge = item.querySelector(".badge");
    badge.className = `badge badge-${status}`;
    badge.textContent = status;
  }
}

function updateProgress(rows) {
  if (!rows) return;
  const total = rows.length;
  const completed = rows.filter(
    (r) => r.call_status === "completed" || r.call_status === "failed" || r.call_status === "no-answer"
  ).length;
  const pct = total > 0 ? (completed / total) * 100 : 0;
  progressBar.style.width = `${pct}%`;
  progressText.textContent = `${completed} / ${total}`;
}

function updateStats(stats) {
  document.getElementById("statCompleted").textContent = stats.completed || 0;
  document.getElementById("statApproved").textContent = stats.approved || 0;
  document.getElementById("statDenied").textContent = stats.denied || 0;
  document.getElementById("statPending").textContent = stats.claim_pending || 0;

  // Update progress bar too
  const total = stats.total || 0;
  const done = (stats.completed || 0) + (stats.failed || 0) + (stats.no_answer || 0);
  const pct = total > 0 ? (done / total) * 100 : 0;
  progressBar.style.width = `${pct}%`;
  progressText.textContent = `${done} / ${total}`;
}

function showLiveCall(claimData) {
  callInfo.style.display = "block";
  document.getElementById("livePatient").textContent = claimData.patient_name || "-";
  document.getElementById("liveClaim").textContent = claimData.claim_number || "-";
  document.getElementById("livePhone").textContent = claimData.insurance_phone || "-";
  transcriptFeed.innerHTML = "";
}

function addTranscriptLine(speaker, text) {
  const line = document.createElement("div");
  const cls = speaker === "Agent" ? "agent" : speaker === "System" ? "system" : "human";
  line.className = `transcript-line ${cls}`;
  line.textContent = `${speaker}: ${text}`;
  transcriptFeed.appendChild(line);
  transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
}

function addResult(claimNumber, results) {
  const item = document.createElement("div");
  item.className = "result-item";
  const resultBadge = results.claim_result || "unknown";
  item.innerHTML = `
    <div class="result-header">
      <span class="claim-id">${claimNumber}</span>
      <span class="badge badge-completed">${resultBadge}</span>
    </div>
    <div class="details">
      ${results.approved_amount ? `Amount: $${results.approved_amount}` : ""}
      ${results.denial_reason ? `Reason: ${results.denial_reason}` : ""}
      ${results.reference_number ? `Ref: ${results.reference_number}` : ""}
      ${results.confirmed === "true" ? " | Confirmed" : ""}
    </div>
    <a href="/api/transcript/${claimNumber}" target="_blank">View Transcript</a>
  `;
  resultsList.prepend(item);
}

function startCallTimer() {
  callStartTime = Date.now();
  callTimer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - callStartTime) / 1000);
    const mins = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const secs = String(elapsed % 60).padStart(2, "0");
    document.getElementById("liveDuration").textContent = `${mins}:${secs}`;
  }, 1000);
}

function stopCallTimer() {
  if (callTimer) {
    clearInterval(callTimer);
    callTimer = null;
  }
}

async function refreshQueue() {
  try {
    const res = await fetch("/api/claims");
    const rows = await res.json();
    renderQueue(rows);
  } catch (e) {
    console.error("Failed to refresh queue:", e);
  }
}

// --- Event Listeners ---

csvUpload.addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append("file", file);

  setStatus("Uploading CSV...");
  try {
    const res = await fetch("/api/upload-csv", { method: "POST", body: formData });
    const data = await res.json();
    setStatus(data.message);
  } catch (err) {
    setStatus("Failed to upload CSV");
    console.error(err);
  }
});

startBtn.addEventListener("click", async () => {
  startBtn.disabled = true;
  pauseBtn.disabled = false;
  stopBtn.disabled = false;
  await fetch("/api/start", { method: "POST" });
});

pauseBtn.addEventListener("click", async () => {
  pauseBtn.disabled = true;
  startBtn.disabled = false;
  await fetch("/api/pause", { method: "POST" });
});

stopBtn.addEventListener("click", async () => {
  stopBtn.disabled = true;
  pauseBtn.disabled = true;
  startBtn.disabled = false;
  await fetch("/api/stop", { method: "POST" });
});

downloadBtn.addEventListener("click", () => {
  window.location.href = "/api/download-csv";
});

// --- Init ---
connectWebSocket();
