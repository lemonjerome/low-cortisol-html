# CSS Skill Specification
File: css.md

## Purpose

CSS controls the visual presentation of elements defined in HTML (html.md).

CSS selectors must reference ONLY IDs and classes from the HTML file.
CSS must define ALL state classes that JavaScript (js.md) toggles.

---

# Core Responsibilities

CSS handles:

- Layout (flexbox, grid)
- Colors, typography, spacing
- Responsiveness
- Visual states (hidden, active, disabled)
- Animations and transitions

CSS does NOT handle:

- DOM manipulation
- Business logic
- Data handling

---

# Standard State Classes (Critical Cross-File Contract)

CSS MUST define these state classes. JS toggles them, CSS styles them.

REQUIRED — always include these in every stylesheet:

.hidden {
  display: none !important;
}

.active — style varies per component (tabs, nav items, etc.):

.nav-item.active {
  border-bottom: 2px solid var(--primary);
  font-weight: bold;
}

.disabled — style varies per component:

.btn.disabled {
  opacity: 0.5;
  pointer-events: none;
  cursor: not-allowed;
}

CRITICAL: .hidden { display: none !important; } MUST be present in EVERY CSS file.
This is the ONLY visibility mechanism. JS uses classList.add("hidden") / classList.remove("hidden").

BANNED — never define these alternative classes:
- .is-open, .is-hidden, .is-visible, .show, .visible, .open, .closed
- .is-active, .is-disabled, .is-selected

---

# Modal / Overlay Styling (Critical)

Modals use the "hidden" class for visibility (toggled by JS).
Do NOT use opacity/visibility transitions for show/hide.

Required pattern:

.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-overlay.hidden {
  display: none !important;
}

.modal-content {
  background: var(--surface, #fff);
  border-radius: 8px;
  padding: 24px;
  max-width: 500px;
  width: 90%;
  max-height: 80vh;
  overflow-y: auto;
}

CRITICAL: The modal starts hidden because HTML has class="modal-overlay hidden".
JS removes "hidden" to show, adds "hidden" to hide.
Do NOT use opacity: 0 / visibility: hidden for modal visibility.
Do NOT define .modal-overlay.is-open — that class does not exist.

---

# Button Classes (Standard)

CSS must define the standard button classes used in HTML and JS:

.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.9rem;
  transition: background 0.2s;
}

.btn-primary {
  background: var(--primary, #4a90d9);
  color: white;
}

.btn-secondary {
  background: var(--secondary, #6c757d);
  color: white;
}

.btn-danger, .btn-delete {
  background: var(--danger, #dc3545);
  color: white;
}

.btn-edit {
  background: var(--info, #17a2b8);
  color: white;
}

NEVER leave button classes (btn, btn-primary, btn-secondary, btn-danger,
btn-edit, btn-delete) unstyled if they appear in the HTML.

---

# Dynamic Element Styling (Critical)

When JS creates elements dynamically (cards, list items, etc.),
CSS must style the EXACT class names used.

Read the completed HTML file for the dynamic element template comment.
Read the completed JS file for the actual class names used in createElement calls.

The class names in HTML, JS, and CSS MUST all match exactly.

Example — if JS creates elements with class "note-card":

.note-card {
  border: 1px solid var(--border, #ddd);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 12px;
}

.note-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.note-card-title {
  font-size: 1.1rem;
  font-weight: bold;
}

.note-card-body {
  margin-bottom: 12px;
}

.note-card-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

CRITICAL: If JS uses "note-card-actions", CSS must style ".note-card-actions"
— NOT ".note-actions", ".note-footer", or any other name.

---

# Selector Rules

All selectors must reference identifiers from the HTML file.

Selector priority:
1. Classes (for reusable styles)
2. Component IDs (for unique elements)
3. Structural selectors (element type, child combinators)

Never create selectors for IDs or classes that do not exist in HTML.

---

# Class Naming Convention

Classes must follow kebab-case:
primary-button, card-container, form-input, sidebar-menu

Avoid vague names: box, thing, stuff, wrapper1

---

# Layout Guidelines

Use modern layout systems:

1. Flexbox — for 1D layouts (rows, columns)
2. Grid — for 2D layouts
3. Block — for simple stacking

---

# Responsive Design

Always support responsive layouts:

@media (max-width: 768px) {
  .note-card-header {
    flex-direction: column;
  }
}

---

# CSS Variables (Recommended)

Define a color scheme using CSS variables for consistency:

:root {
  --primary: #4a90d9;
  --secondary: #6c757d;
  --danger: #dc3545;
  --info: #17a2b8;
  --success: #28a745;
  --bg: #1a1a2e;
  --surface: #16213e;
  --text: #e0e0e0;
  --border: #2a2a4a;
}

---

# Animation Rules

Animations should be defined in CSS and triggered by class changes from JS.

.fade-in {
  animation: fadeIn 0.3s ease-in;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

---

# Integration Contract

## With HTML (html.md)
- CSS must style ALL classes and IDs from the HTML file
- CSS must NOT create selectors for elements that don't exist in HTML
- CSS must define .hidden { display: none !important; }

## With JavaScript (js.md)
- CSS must define styles for every class that JS toggles (hidden, active, disabled)
- CSS must style every class that JS uses in dynamic element creation
- Match class names EXACTLY — read the completed JS to verify

---

# Summary

CSS controls:
- Layout, appearance, and visual states

CSS MUST define:
- .hidden { display: none !important; } — ALWAYS
- Styles for all standard button classes (btn, btn-primary, etc.)
- Styles for all dynamic element classes (from JS createElement)
- Styles for all IDs and classes in HTML

CSS depends on:
- Identifiers defined in html.md (source of truth)
- State class toggles from js.md (hidden, active, disabled ONLY)