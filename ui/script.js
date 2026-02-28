const state = {
  working: false,
  hasProject: false,
  currentAssistantBubble: null,
  currentThinkingEl: null,
  currentWorkspacesRoot: "",
  chatAbortController: null,
  folderChooserAvailable: true,
  folderChooserReason: "",
  lastStatusLabel: "",
  stopAbortTimeoutId: null,
};

const reasoningStreamNodes = new Map();
let reasoningStreamAutoId = 0;

const chatStream = document.getElementById("chatStream");
const actionsStream = document.getElementById("actionsStream");
const reasoningStream = document.getElementById("reasoningStream");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const sendBtn = document.getElementById("sendBtn");
const projectIndicator = document.getElementById("projectIndicator");

function updateProjectIndicator(projectName) {
  if (!projectIndicator) return;
  if (projectName) {
    state.hasProject = true;
    projectIndicator.textContent = "\u25cf " + projectName;
    projectIndicator.className = "project-indicator project-indicator-active";
    chatInput.disabled = false;
    chatInput.placeholder = "Describe what you want to build or change...";
    if (!state.working) sendBtn.disabled = false;
  } else {
    state.hasProject = false;
    projectIndicator.textContent = "No project loaded";
    projectIndicator.className = "project-indicator project-indicator-none";
    chatInput.disabled = true;
    chatInput.placeholder = "Open or create a project to start...";
    sendBtn.disabled = true;
  }
}

function setWorking(working) {
  state.working = working;
  sendBtn.disabled = working ? false : !state.hasProject;
  chatInput.disabled = working || !state.hasProject;
  sendBtn.textContent = working ? "Stop" : "Send";
  sendBtn.classList.toggle("danger", working);
  sendBtn.classList.toggle("primary", !working);
}

function showModal(id) {
  document.getElementById(id).classList.add("visible");
}

function hideModal(id) {
  document.getElementById(id).classList.remove("visible");
}

function appendAction(text, kind = "default") {
  const item = document.createElement("div");
  item.className = `action-item action-${kind}`;
  item.textContent = text;
  actionsStream.appendChild(item);
  actionsStream.scrollTop = actionsStream.scrollHeight;
}

function summarizeToolArguments(tool, args = {}) {
  if (!args || typeof args !== "object") return "{}";
  if (tool === "create_file") {
    const rel = String(args.relative_path || "").trim();
    const overwrite = Boolean(args.overwrite);
    return JSON.stringify({ relative_path: rel, overwrite });
  }
  if (tool === "append_to_file") {
    return JSON.stringify({ relative_path: String(args.relative_path || "").trim() });
  }
  if (tool === "insert_after_marker") {
    return JSON.stringify({
      relative_path: String(args.relative_path || "").trim(),
      marker: String(args.marker || "").trim().slice(0, 60),
      occurrence: String(args.occurrence || "first"),
    });
  }
  if (tool === "replace_range") {
    return JSON.stringify({
      relative_path: String(args.relative_path || "").trim(),
      start_line: args.start_line,
      end_line: args.end_line,
    });
  }
  if (tool === "read_file") {
    return JSON.stringify({ relative_path: String(args.relative_path || "").trim() });
  }
  if (tool === "list_directory") {
    return JSON.stringify({ relative_path: String(args.relative_path || ".").trim() || "." });
  }
  if (tool === "validate_web_app") {
    return JSON.stringify({ app_dir: String(args.app_dir || "").trim() || "." });
  }
  if (tool === "run_unit_tests") {
    return JSON.stringify({ test_file: String(args.test_file || "").trim(), timeout_seconds: args.timeout_seconds });
  }
  if (tool === "plan_web_build") {
    const summary = String(args.summary || "").trim();
    return JSON.stringify({ summary: summary.slice(0, 80) + (summary.length > 80 ? "..." : "") });
  }
  return JSON.stringify(args);
}

function renderMarkdown(text) {
  // Lightweight markdown → HTML renderer for reasoning display.
  // Handles: headers, bold, italic, inline code, lists, paragraphs.
  let html = text
    // Escape HTML entities first
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Fenced code blocks ```lang\n...\n```
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre class="md-code-block"><code>${code.trim()}</code></pre>`;
  });

  // Split into lines for block-level parsing
  const lines = html.split("\n");
  const out = [];
  let inList = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Headers ### → <strong class="md-heading">
    const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      if (inList) { out.push("</ul>"); inList = false; }
      const level = headingMatch[1].length;
      out.push(`<strong class="md-h${level}">${headingMatch[2]}</strong>`);
      continue;
    }

    // Unordered list items: - item or * item
    const listMatch = line.match(/^\s*[-*]\s+(.+)$/);
    if (listMatch) {
      if (!inList) { out.push('<ul class="md-list">'); inList = true; }
      out.push(`<li>${listMatch[1]}</li>`);
      continue;
    }

    // Numbered list items: 1. item
    const numMatch = line.match(/^\s*\d+\.\s+(.+)$/);
    if (numMatch) {
      if (!inList) { out.push('<ul class="md-list md-list-num">'); inList = true; }
      out.push(`<li>${numMatch[1]}</li>`);
      continue;
    }

    if (inList) { out.push("</ul>"); inList = false; }

    // Empty line → small spacer
    if (!line.trim()) {
      out.push('<div class="md-spacer"></div>');
      continue;
    }

    out.push(`<span>${line}</span>`);
  }
  if (inList) out.push("</ul>");

  html = out.join("\n");

  // Inline styles
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  html = html.replace(/`([^`]+)`/g, '<code class="md-inline-code">$1</code>');

  return html;
}

function appendReasoning(stage, text) {
  if (!text) return;
  const item = document.createElement("div");
  const normalizedStage = String(stage || "state").toLowerCase();
  item.className = `reason-item reason-${normalizedStage}`;

  // Code blocks get special rendering
  if (normalizedStage === "code") {
    const lines = text.split("\n");
    const firstLine = lines[0] || "";
    // First line is "[code] filename", rest is code body
    const labelMatch = firstLine.match(/^\[code\]\s*(.*)$/i);
    const label = labelMatch ? labelMatch[1].trim() : "code";
    const codeBody = lines.slice(1).join("\n").trim();

    // Skip empty code blocks entirely (no body to display)
    if (!codeBody) return;

    const header = document.createElement("div");
    header.className = "reason-code-header";
    header.textContent = `[code] ${label}`;
    item.appendChild(header);

    if (codeBody) {
      const pre = document.createElement("pre");
      pre.className = "reason-code-body";
      const codeEl = document.createElement("code");
      codeEl.textContent = codeBody;
      pre.appendChild(codeEl);
      item.appendChild(pre);
    }
  } else {
    // Render markdown for all text reasoning
    const stageLabel = document.createElement("span");
    stageLabel.className = "reason-stage-label";
    stageLabel.textContent = `[${stage}] `;
    item.appendChild(stageLabel);

    const content = document.createElement("div");
    content.className = "reason-content";
    content.innerHTML = renderMarkdown(text);
    item.appendChild(content);
  }

  reasoningStream.appendChild(item);
  reasoningStream.scrollTop = reasoningStream.scrollHeight;
}

function ensureReasoningStreamNode(streamId, stage) {
  if (!streamId) return null;
  if (reasoningStreamNodes.has(streamId)) {
    return reasoningStreamNodes.get(streamId);
  }
  const item = document.createElement("div");
  const normalizedStage = String(stage || "state").toLowerCase();
  item.className = `reason-item reason-${normalizedStage}`;
  item.classList.add("reason-streaming");
  item.textContent = `[${stage}] `;
  reasoningStream.appendChild(item);
  reasoningStream.scrollTop = reasoningStream.scrollHeight;
  const node = { item, stage };
  reasoningStreamNodes.set(streamId, node);
  return node;
}

function appendReasoningStreamToken(event) {
  const token = String(event.token || "").toLowerCase();
  const streamId = String(event.stream_id || "").trim();
  const stage = event.stage || "state";
  if (!streamId) return;
  if (token === "start") {
    ensureReasoningStreamNode(streamId, stage);
    return;
  }
  const node = ensureReasoningStreamNode(streamId, stage);
  if (!node) return;
  if (token === "chunk") {
    node.item.textContent += String(event.text || "");
    reasoningStream.scrollTop = reasoningStream.scrollHeight;
    return;
  }
  if (token === "word") {
    node.item.textContent += String(event.text || "");
    reasoningStream.scrollTop = reasoningStream.scrollHeight;
    return;
  }
  if (token === "end") {
    node.item.classList.remove("reason-streaming");
    reasoningStreamNodes.delete(streamId);
  }
}

function streamReasoningText(stage, text) {
  const cleaned = String(text || "");
  if (!cleaned.trim()) return;
  reasoningStreamAutoId += 1;
  const streamId = `legacy-${String(stage || "state").toLowerCase()}-${reasoningStreamAutoId}`;
  appendReasoningStreamToken({ token: "start", stream_id: streamId, stage });
  const parts = cleaned.match(/\S+\s*/g) || [];
  for (const part of parts) {
    appendReasoningStreamToken({ token: "word", stream_id: streamId, stage, text: part });
  }
  appendReasoningStreamToken({ token: "end", stream_id: streamId, stage });
}

function closeAllReasoningStreams() {
  for (const node of reasoningStreamNodes.values()) {
    node.item.classList.remove("reason-streaming");
  }
  reasoningStreamNodes.clear();
}

function createChatRow(userText) {
  const row = document.createElement("div");
  row.className = "chat-row";

  const user = document.createElement("div");
  user.className = "user-bubble";
  user.textContent = userText;

  const assistant = document.createElement("div");
  assistant.className = "agent-output";
  assistant.textContent = "";

  const thinking = document.createElement("div");
  thinking.className = "thinking";
  thinking.textContent = "thinking...";

  row.appendChild(user);
  row.appendChild(assistant);
  row.appendChild(thinking);
  chatStream.appendChild(row);
  chatStream.scrollTop = chatStream.scrollHeight;

  state.currentAssistantBubble = assistant;
  state.currentThinkingEl = thinking;
}

function clearUiMemory() {
  chatStream.innerHTML = "";
  actionsStream.innerHTML = "";
  reasoningStream.innerHTML = "";
  state.currentAssistantBubble = null;
  state.currentThinkingEl = null;
  closeAllReasoningStreams();
}

function clearStopAbortTimeout() {
  if (state.stopAbortTimeoutId) {
    window.clearTimeout(state.stopAbortTimeoutId);
    state.stopAbortTimeoutId = null;
  }
}

async function apiPost(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) {
    throw new Error(data?.error?.message || data?.error || "Request failed");
  }
  return data;
}

// ---- Project picker (loads lch_ folders from lch_workspaces) ----
async function _loadProjectPicker() {
  const listEl = document.getElementById("projectPickerList");
  const errEl = document.getElementById("openProjectError");
  listEl.innerHTML = '<div class="project-picker-loading">Loading projects...</div>';
  errEl.textContent = "";
  try {
    const res = await fetch("/api/browse-dir?path=" + encodeURIComponent(state.currentWorkspacesRoot || ""));
    const data = await res.json();
    if (!data.ok) { errEl.textContent = data.error || "Could not read lch_workspaces"; listEl.innerHTML = ""; return; }
    const dirs = data.entries.filter(e => e.is_dir && e.name.startsWith("lch_"));
    listEl.innerHTML = "";
    if (dirs.length === 0) {
      listEl.innerHTML = '<div class="project-picker-empty">No projects found.<br>Use <strong>New Project</strong> to create one.</div>';
      return;
    }
    for (const entry of dirs) {
      const row = document.createElement("div");
      row.className = "project-picker-item";
      row.textContent = entry.name;
      row.addEventListener("click", async () => {
        errEl.textContent = "";
        const projectPath = data.path + "/" + entry.name;
        try {
          await apiPost("/api/open-project", { projectPath });
          clearUiMemory();
          hideModal("openProjectModal");
          updateProjectIndicator(entry.name);
          appendAction("project opened: " + entry.name, "system");
        } catch (err) {
          errEl.textContent = err.message;
        }
      });
      listEl.appendChild(row);
    }
  } catch (err) {
    errEl.textContent = err.message || "Network error";
    listEl.innerHTML = "";
  }
}



async function loadStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  state.currentWorkspacesRoot = data.workspaces_root || "";
  updateProjectIndicator(data.current_project_name || null);
}

async function submitChat(message) {
  setWorking(true);
  state.lastStatusLabel = "";
  createChatRow(message);
  state.chatAbortController = new AbortController();

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal: state.chatAbortController.signal,
  });

  if (!response.ok) {
    const data = await response.json();
    throw new Error(data?.error?.message || "Chat failed");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n");
    while (boundary !== -1) {
      const line = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 1);
      boundary = buffer.indexOf("\n");
      if (!line) continue;

      const event = JSON.parse(line);
      if (event.type === "status" && state.currentThinkingEl) {
        state.currentThinkingEl.textContent = event.label || "working...";
        const label = String(event.label || "").trim();
        if (label && label !== state.lastStatusLabel) {
          appendAction(`status: ${label}`, "status");
          state.lastStatusLabel = label;
        }
      }
      if (event.type === "reasoning") {
        const reasoningText = String(event.text || "").trim();
        if (reasoningText && reasoningText !== "```json" && reasoningText !== "```") {
          appendReasoning(event.stage || "state", reasoningText);
        }
      }
      if (event.type === "reasoning_stream") {
        appendReasoningStreamToken(event);
      }
      if (event.type === "action") {
        if (event.tool === "file_edit") {
          appendAction(`file updated: ${event.arguments.relative_path}`, "file");
        } else {
          appendAction(`tool: ${event.tool} ${summarizeToolArguments(event.tool, event.arguments || {})}`, "tool");
        }
      }
      if (event.type === "chat_chunk" && state.currentAssistantBubble) {
        state.currentAssistantBubble.textContent = event.text || "";
      }
      if (event.type === "chat_final" && state.currentAssistantBubble) {
        state.currentAssistantBubble.innerHTML = renderMarkdown(event.text || "");
        state.currentAssistantBubble.classList.add("agent-markdown");
      }
      if (event.type === "error" && state.currentAssistantBubble) {
        state.currentAssistantBubble.textContent = `Error: ${event.message || "Unknown error"}`;
        appendAction(`error: ${event.message || "Unknown error"}`, "error");
        closeAllReasoningStreams();
      }
      if (event.type === "stopped") {
        appendAction(event.message || "execution stopped", "stopped");
        if (state.currentAssistantBubble) {
          state.currentAssistantBubble.textContent = event.message || "Execution stopped by user.";
        }
        if (state.currentThinkingEl) {
          state.currentThinkingEl.remove();
          state.currentThinkingEl = null;
        }
        closeAllReasoningStreams();
      }
      if (event.type === "done") {
        if (state.currentThinkingEl) {
          state.currentThinkingEl.remove();
          state.currentThinkingEl = null;
        }
        closeAllReasoningStreams();
      }
    }
  }

  state.chatAbortController = null;
  clearStopAbortTimeout();
  setWorking(false);
}

async function stopCurrentRun() {
  appendAction("stop requested", "stopped");
  try {
    await apiPost("/api/stop", {});
  } catch (error) {
    appendAction(`stop error: ${error.message}`, "error");
  }
  clearStopAbortTimeout();
  state.stopAbortTimeoutId = window.setTimeout(() => {
    if (state.chatAbortController) {
      state.chatAbortController.abort();
    }
  }, 2500);
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.working) {
    await stopCurrentRun();
    return;
  }
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";

  try {
    await submitChat(message);
  } catch (error) {
    if (error.name === "AbortError") {
      appendAction("execution aborted in browser", "stopped");
      if (state.currentThinkingEl) {
        state.currentThinkingEl.remove();
        state.currentThinkingEl = null;
      }
      state.chatAbortController = null;
      clearStopAbortTimeout();
      setWorking(false);
      return;
    }
    state.chatAbortController = null;
    clearStopAbortTimeout();
    setWorking(false);
    appendAction(`error: ${error.message}`, "error");
    if (state.currentThinkingEl) {
      state.currentThinkingEl.remove();
      state.currentThinkingEl = null;
    }
    if (state.currentAssistantBubble) {
      state.currentAssistantBubble.textContent = `Error: ${error.message}`;
    }
  }
});

chatInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") {
    return;
  }
  if (event.shiftKey) {
    return;
  }
  if (state.working) {
    event.preventDefault();
    return;
  }
  event.preventDefault();
  chatForm.requestSubmit();
});

document.getElementById("helpBtn").addEventListener("click", () => showModal("tutorialModal"));
document.getElementById("tutorialClose").addEventListener("click", () => {
  sessionStorage.setItem("tutorialSeen", "1");
  hideModal("tutorialModal");
});
document.getElementById("tutorialGotIt").addEventListener("click", () => {
  sessionStorage.setItem("tutorialSeen", "1");
  hideModal("tutorialModal");
});

document.getElementById("newProjectBtn").addEventListener("click", () => {
  document.getElementById("newWorkspaceName").value = "lch_new_project";
  document.getElementById("newProjectError").textContent = "";
  showModal("newProjectModal");
});

document.getElementById("newProjectCancel").addEventListener("click", () => hideModal("newProjectModal"));

document.getElementById("newProjectCreate").addEventListener("click", async () => {
  const errorEl = document.getElementById("newProjectError");
  errorEl.textContent = "";
  try {
    const workspaceName = document.getElementById("newWorkspaceName").value.trim();
    if (!workspaceName.startsWith("lch_")) {
      throw new Error("Folder name must start with 'lch_'.");
    }
    // Always create inside lch_workspaces
    await apiPost("/api/create-project", {
      parentDir: state.currentWorkspacesRoot,
      workspaceName,
    });
    clearUiMemory();
    hideModal("newProjectModal");
    updateProjectIndicator(workspaceName);
    appendAction("new project created: " + workspaceName, "system");
  } catch (error) {
    errorEl.textContent = error.message;
  }
});

document.getElementById("openProjectBtn").addEventListener("click", () => {
  document.getElementById("openProjectError").textContent = "";
  showModal("openProjectModal");
  _loadProjectPicker();
});

document.getElementById("openProjectCancel").addEventListener("click", () => hideModal("openProjectModal"));

document.getElementById("clearChatBtn").addEventListener("click", () => showModal("clearChatModal"));
document.getElementById("clearChatCancel").addEventListener("click", () => hideModal("clearChatModal"));

document.getElementById("clearChatConfirm").addEventListener("click", async () => {
  await apiPost("/api/clear-chat", {});
  clearUiMemory();
  hideModal("clearChatModal");
  appendAction("chat memory cleared", "system");
});

document.getElementById("openHtmlBtn").addEventListener("click", async () => {
  try {
    const data = await apiPost("/api/open-main-html", {});
    appendAction(`opened main html: ${data.main_html}`, "system");
    if (data.workspace_url) {
      window.open(data.workspace_url, "_blank", "noopener,noreferrer");
    }
  } catch (error) {
    appendAction(`open html error: ${error.message}`, "error");
  }
});

(async () => {
  try {
    await loadStatus();
  } catch (error) {
    appendAction(`startup error: ${error.message}`, "error");
  }
  // Show tutorial on first visit (once per browser session)
  const tutorialSeen = sessionStorage.getItem("tutorialSeen");
  if (!tutorialSeen) {
    showModal("tutorialModal");
  }
})();

window.addEventListener("beforeunload", (event) => {
  const hasChat = chatStream.children.length > 0;
  if (!hasChat && !state.working) {
    return;
  }
  event.preventDefault();
  event.returnValue = "Chat is not saved and will be lost if you close this tab.";
});
