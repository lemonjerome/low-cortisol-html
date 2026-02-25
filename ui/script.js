const state = {
  working: false,
  currentAssistantBubble: null,
  currentThinkingEl: null,
  currentWorkspacesRoot: "",
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

function setWorking(working) {
  state.working = working;
  sendBtn.disabled = working;
  chatInput.disabled = working;
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
  const data = await apiPost("/api/choose-folder", {});
  return data.path;
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  startupRootInput.value = data.workspaces_root || "";
  state.currentWorkspacesRoot = data.workspaces_root || "";
}

async function submitChat(message) {
  setWorking(true);
  createChatRow(message);

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
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
      }
      if (event.type === "reasoning") {
        appendReasoning(event.stage || "state", event.text || "");
      }
      if (event.type === "action") {
        if (event.tool === "file_edit") {
          appendAction(`file updated: ${event.arguments.relative_path}`);
        } else {
          appendAction(`tool: ${event.tool} ${JSON.stringify(event.arguments || {})}`);
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
      if (event.type === "done") {
        if (state.currentThinkingEl) {
          state.currentThinkingEl.remove();
          state.currentThinkingEl = null;
        }
      }
    }
  }

  setWorking(false);
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.working) return;
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";

  try {
    await submitChat(message);
  } catch (error) {
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
    const data = await apiPost("/api/set-workspaces-root", { path: startupRootInput.value.trim() });
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
    await apiPost("/api/create-project", {
      parentDir: document.getElementById("newParentInput").value.trim(),
      workspaceName: document.getElementById("newWorkspaceName").value.trim(),
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
    await apiPost("/api/open-project", {
      projectPath: document.getElementById("openProjectPath").value.trim(),
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
