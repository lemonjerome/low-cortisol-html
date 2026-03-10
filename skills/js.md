# JavaScript Skill Specification
File: js.md

## Purpose

JavaScript controls behavior and interactivity of elements defined in HTML (html.md).

JavaScript must NOT control styling directly.
JavaScript toggles ONLY the standard state classes defined in the cross-file contract.

---

# Core Responsibilities

JavaScript handles:

- Event handling
- DOM interaction
- UI state toggling
- Data management (localStorage, arrays, objects)
- Dynamic element creation

JavaScript must NOT:

- Write inline CSS (no element.style.xxx)
- Hardcode styles
- Invent new class names not defined in HTML
- Use non-standard toggle classes

---

# Element Selection Rules

JavaScript must reference EXACT IDs defined in html.md.

Preferred selectors:

document.getElementById("exact-id-from-html")
document.querySelector(".exact-class-from-html")

Every ID used in JS MUST exist in the HTML file.

---

# Standard State Classes (Critical Cross-File Contract)

These are the ONLY classes JS is allowed to toggle.
All other class names are for structure/styling only (set in HTML, styled in CSS).

VISIBILITY TOGGLE:
  el.classList.add("hidden")     — hides the element
  el.classList.remove("hidden")  — shows the element

ACTIVE STATE TOGGLE:
  el.classList.add("active")     — marks as active
  el.classList.remove("active")  — marks as inactive

DISABLED STATE TOGGLE:
  el.classList.add("disabled")   — marks as disabled
  el.classList.remove("disabled") — marks as enabled

BANNED (never use these):
  "is-open", "is-hidden", "is-visible", "show", "visible", "open", "closed"
  "is-active", "is-disabled", "is-selected"

Example — showing a modal:
  const modal = document.getElementById("add-modal");
  modal.classList.remove("hidden");  // show

Example — hiding a modal:
  modal.classList.add("hidden");  // hide

---

# Modal Toggle Pattern (Critical)

Every modal open/close MUST follow this exact pattern:

function openModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) modal.classList.remove("hidden");
}

function closeModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) modal.classList.add("hidden");
}

// Usage:
openBtn.addEventListener("click", () => openModal("add-note-modal"));
cancelBtn.addEventListener("click", () => closeModal("add-note-modal"));

NEVER toggle "is-open", "show", "visible", or any other class for modals.

---

# Dynamic Element Creation (Critical)

When creating elements dynamically, use ONLY class names that are:
1. Documented in the HTML dynamic element template comment
2. Styled in the CSS file

Example — creating a note card:

function createNoteCard(note) {
  const card = document.createElement("article");
  card.className = "note-card";

  card.innerHTML = `
    <div class="note-card-header">
      <h3 class="note-card-title">${escapeHtml(note.title)}</h3>
      <span class="note-card-date">${note.date}</span>
    </div>
    <div class="note-card-body">
      <p>${escapeHtml(note.content)}</p>
    </div>
    <div class="note-card-actions">
      <button class="btn btn-edit" data-id="${note.id}">Edit</button>
      <button class="btn btn-delete" data-id="${note.id}">Delete</button>
    </div>
  `;
  return card;
}

CRITICAL: class names like "note-card", "note-card-header", "note-card-body",
"note-card-actions", "btn", "btn-edit", "btn-delete" MUST match exactly
what CSS styles and what the HTML template documents.

Do NOT use different names like "note-footer", "note-actions", "btn-small", etc.
if those are not the names defined in HTML and styled in CSS.

---

# Button Classes (Standard)

Use these standard button classes (same as html.md):

- btn            — base button
- btn-primary    — primary action
- btn-secondary  — secondary action
- btn-danger     — destructive action
- btn-edit       — edit action
- btn-delete     — alias for btn-danger

NEVER use: btn-small, btn-sm, btn-xs, or size variants not in the CSS.

---

# Event Handling

Attach event listeners for interactivity.

document.addEventListener("DOMContentLoaded", () => {
  const loginButton = document.getElementById("login-button");
  if (loginButton) {
    loginButton.addEventListener("click", handleLogin);
  }
});

Use event delegation for dynamic elements:

container.addEventListener("click", (e) => {
  if (e.target.closest(".btn-delete")) {
    const id = e.target.closest(".btn-delete").dataset.id;
    deleteItem(id);
  }
});

---

# Edit State Tracking (Critical)

When an app has edit functionality (e.g., edit a note, edit a todo),
JS must track WHICH item is being edited.

Pattern — use a module-level variable:

let currentEditId = null;

function openEditModal(itemId) {
  currentEditId = itemId;
  const item = getItemById(itemId);
  document.getElementById("edit-title-input").value = item.title;
  document.getElementById("edit-body-textarea").value = item.content;
  document.getElementById("edit-modal").classList.remove("hidden");
}

function handleEditSubmit(e) {
  e.preventDefault();
  if (!currentEditId) return;
  const title = document.getElementById("edit-title-input").value.trim();
  const content = document.getElementById("edit-body-textarea").value.trim();
  updateItem(currentEditId, title, content);
  currentEditId = null;
  document.getElementById("edit-modal").classList.add("hidden");
}

CRITICAL: Do NOT try to read the edit ID from form.dataset or e.target.dataset
unless you explicitly SET it when opening the modal.
The safest pattern is a module-level variable like `currentEditId`.

---

# DOM Safety

Always check elements exist before using them.

const el = document.getElementById("my-element");
if (el) {
  el.addEventListener("click", handler);
}

---

# Data Management

For persistent data, use localStorage:

function saveNotes(notes) {
  localStorage.setItem("notes", JSON.stringify(notes));
}

function loadNotes() {
  const stored = localStorage.getItem("notes");
  return stored ? JSON.parse(stored) : [];
}

---

# HTML Escaping

Always escape user input before inserting into innerHTML:

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

When to use escapeHtml:
- INSIDE innerHTML or template literals: ${escapeHtml(note.title)}
- INSIDE dynamically built HTML strings

When NOT to use escapeHtml:
- Setting .value on input/textarea: input.value = note.title (plain text, no HTML)
- Setting .textContent: el.textContent = note.title (already safe)

INCORRECT (double-escapes, turns & into &amp;):
  document.getElementById("edit-title-input").value = escapeHtml(title);

CORRECT:
  document.getElementById("edit-title-input").value = title;

---

# Performance Guidelines

- Cache DOM selectors at the top of DOMContentLoaded
- Use event delegation for lists of dynamic elements
- Avoid excessive DOM queries inside loops

---

# Summary

JavaScript controls:

- Behavior and events
- UI state via standard toggle classes ONLY (hidden, active, disabled)
- Dynamic element creation using ONLY class names from HTML
- Data management (localStorage, state arrays)

JavaScript must rely on:

- EXACT IDs defined in html.md
- EXACT class names defined in html.md
- ONLY standard state classes for toggling (hidden, active, disabled)