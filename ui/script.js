const state = {
  working: false,
  currentAssistantBubble: null,
  currentThinkingEl: null,
  currentWorkspacesRoot: "",
  chatAbortController: null,
  folderChooserAvailable: true,
  folderChooserReason: "",
  lastStatusLabel: "",
};

const chatStream = document.getElementById("chatStream");
const actionsStream = document.getElementById("actionsStream");
const reasoningStream = document.getElementById("reasoningStream");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const sendBtn = document.getElementById("sendBtn");

const startupModal = document.getElementById("startupModal");
const startupRootInput = document.getElementById("startupRootInput");
const startupError = document.getElementById("startupError");
const chooseButtons = [
  document.getElementById("startupChooseBtn"),
  document.getElementById("newParentChooseBtn"),
  document.getElementById("openProjectChoose"),
];

function startsWithLch(pathText) {
  const normalized = (pathText || "").trim().replace(/[\\/]+$/, "");
  if (!normalized) return false;
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  const name = parts[parts.length - 1] || "";
  return name.startsWith("lch_");
}

function setWorking(working) {
  state.working = working;
  sendBtn.disabled = false;
  chatInput.disabled = working;
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

function appendAction(text) {
  const item = document.createElement("div");
  item.className = "action-item";
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

function appendReasoning(stage, text) {
  if (!text) return;
  const item = document.createElement("div");
  item.className = "reason-item";
  item.textContent = `[${stage}] ${text}`;
  reasoningStream.appendChild(item);
  reasoningStream.scrollTop = reasoningStream.scrollHeight;
}

function createChatRow(userText) {
  const row = document.createElement("div");
  row.className = "chat-row";

  const user = document.createElement("div");
  user.className = "user-bubble";
  user.textContent = userText;

  const assistant = document.createElement("div");
  assistant.className = "assistant-bubble";
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

async function chooseFolder() {
  if (!state.folderChooserAvailable) {
    throw new Error(state.folderChooserReason || "Folder chooser unavailable in this runtime.");
  }
  const data = await apiPost("/api/choose-folder", {});
  return data.path;
}

function applyFolderChooserAvailability() {
  for (const button of chooseButtons) {
    if (!button) continue;
    button.disabled = !state.folderChooserAvailable;
    button.title = state.folderChooserAvailable
      ? "Open folder picker"
      : state.folderChooserReason || "Folder chooser unavailable in this runtime.";
  }
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  startupRootInput.value = data.workspaces_root || "";
  state.currentWorkspacesRoot = data.workspaces_root || "";
  state.folderChooserAvailable = Boolean(data.folder_chooser_available);
  state.folderChooserReason = data.folder_chooser_reason || "";
  applyFolderChooserAvailability();
  if (!state.folderChooserAvailable && startupError) {
    startupError.textContent = state.folderChooserReason;
  }
}

async function submitChat(message) {
  setWorking(true);
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
          appendAction(`status: ${label}`);
          state.lastStatusLabel = label;
        }
      }
      if (event.type === "reasoning") {
        const reasoningText = String(event.text || "").trim();
        if (reasoningText && reasoningText !== "```json" && reasoningText !== "```") {
          appendReasoning(event.stage || "state", reasoningText);
        }
      }
      if (event.type === "action") {
        if (event.tool === "file_edit") {
          appendAction(`file updated: ${event.arguments.relative_path}`);
        } else {
          appendAction(`tool: ${event.tool} ${summarizeToolArguments(event.tool, event.arguments || {})}`);
        }
      }
      if (event.type === "chat_chunk" && state.currentAssistantBubble) {
        state.currentAssistantBubble.textContent = event.text || "";
      }
      if (event.type === "chat_final" && state.currentAssistantBubble) {
        state.currentAssistantBubble.textContent = event.text || "";
      }
      if (event.type === "error" && state.currentAssistantBubble) {
        state.currentAssistantBubble.textContent = `Error: ${event.message || "Unknown error"}`;
      }
      if (event.type === "stopped") {
        appendAction(event.message || "execution stopped");
        if (state.currentAssistantBubble) {
          state.currentAssistantBubble.textContent = event.message || "Execution stopped by user.";
        }
      }
      if (event.type === "done") {
        if (state.currentThinkingEl) {
          state.currentThinkingEl.remove();
          state.currentThinkingEl = null;
        }
      }
    }
  }

  state.chatAbortController = null;
  setWorking(false);
}

async function stopCurrentRun() {
  try {
    await apiPost("/api/stop", {});
  } catch (error) {
    appendAction(`stop error: ${error.message}`);
  }
  if (state.chatAbortController) {
    state.chatAbortController.abort();
  }
  setWorking(false);
  appendAction("stop requested");
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
      appendAction("execution aborted in browser");
      return;
    }
    state.chatAbortController = null;
    setWorking(false);
    appendAction(`error: ${error.message}`);
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

document.getElementById("startupChooseBtn").addEventListener("click", async () => {
  try {
    startupRootInput.value = await chooseFolder();
  } catch (error) {
    startupError.textContent = error.message;
  }
});

document.getElementById("startupValidateBtn").addEventListener("click", async () => {
  startupError.textContent = "";
  try {
    const startupPath = startupRootInput.value.trim();
    if (!startsWithLch(startupPath)) {
      throw new Error("Warning: Workspace parent directory must start with 'lch_'.");
    }
    const data = await apiPost("/api/set-workspaces-root", { path: startupPath });
    state.currentWorkspacesRoot = data.workspaces_root;
    hideModal("startupModal");
  } catch (error) {
    startupError.textContent = error.message;
  }
});

document.getElementById("newProjectBtn").addEventListener("click", () => {
  document.getElementById("newParentInput").value = state.currentWorkspacesRoot;
  document.getElementById("newWorkspaceName").value = "lch_new_project";
  document.getElementById("newProjectError").textContent = "";
  showModal("newProjectModal");
});

document.getElementById("newProjectCancel").addEventListener("click", () => hideModal("newProjectModal"));

document.getElementById("newParentChooseBtn").addEventListener("click", async () => {
  try {
    document.getElementById("newParentInput").value = await chooseFolder();
  } catch (error) {
    document.getElementById("newProjectError").textContent = error.message;
  }
});

document.getElementById("newProjectCreate").addEventListener("click", async () => {
  const errorEl = document.getElementById("newProjectError");
  errorEl.textContent = "";
  try {
    const parentDir = document.getElementById("newParentInput").value.trim();
    const workspaceName = document.getElementById("newWorkspaceName").value.trim();
    if (!startsWithLch(parentDir)) {
      throw new Error("Warning: Parent directory must start with 'lch_'.");
    }
    if (!workspaceName.startsWith("lch_")) {
      throw new Error("Warning: Workspace directory must start with 'lch_'.");
    }
    await apiPost("/api/create-project", {
      parentDir,
      workspaceName,
    });
    clearUiMemory();
    hideModal("newProjectModal");
    appendAction("new project created and loaded");
  } catch (error) {
    errorEl.textContent = error.message;
  }
});

document.getElementById("openProjectBtn").addEventListener("click", () => {
  document.getElementById("openProjectPath").value = "";
  document.getElementById("openProjectError").textContent = "";
  showModal("openProjectModal");
});

document.getElementById("openProjectCancel").addEventListener("click", () => hideModal("openProjectModal"));

document.getElementById("openProjectChoose").addEventListener("click", async () => {
  try {
    document.getElementById("openProjectPath").value = await chooseFolder();
  } catch (error) {
    document.getElementById("openProjectError").textContent = error.message;
  }
});

document.getElementById("openProjectConfirm").addEventListener("click", async () => {
  const errorEl = document.getElementById("openProjectError");
  errorEl.textContent = "";
  try {
    const projectPath = document.getElementById("openProjectPath").value.trim();
    if (!startsWithLch(projectPath)) {
      throw new Error("Warning: Project directory must start with 'lch_'.");
    }
    await apiPost("/api/open-project", {
      projectPath,
    });
    clearUiMemory();
    hideModal("openProjectModal");
    appendAction("project opened and memory reset");
  } catch (error) {
    errorEl.textContent = error.message;
  }
});

document.getElementById("clearChatBtn").addEventListener("click", () => showModal("clearChatModal"));
document.getElementById("clearChatCancel").addEventListener("click", () => hideModal("clearChatModal"));

document.getElementById("clearChatConfirm").addEventListener("click", async () => {
  await apiPost("/api/clear-chat", {});
  clearUiMemory();
  hideModal("clearChatModal");
  appendAction("chat memory cleared");
});

document.getElementById("openHtmlBtn").addEventListener("click", async () => {
  try {
    const data = await apiPost("/api/open-main-html", {});
    appendAction(`opened main html: ${data.main_html}`);
    if (data.workspace_url) {
      window.open(data.workspace_url, "_blank", "noopener,noreferrer");
    }
  } catch (error) {
    appendAction(`open html error: ${error.message}`);
  }
});

(async () => {
  try {
    await loadStatus();
  } catch (error) {
    startupError.textContent = error.message;
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
