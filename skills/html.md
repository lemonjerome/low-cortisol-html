# HTML Skill Specification
File: html.md

## Purpose
HTML defines the structure, semantics, and identifiers of the webpage.
All IDs, classes, and structural hierarchy originate here.
CSS (css.md) and JavaScript (js.md) MUST reference identifiers defined here.

HTML must never contain styling or behavior logic.

---

# Core Responsibilities

HTML is responsible for:

1. Semantic structure
2. Accessibility
3. Stable identifiers (IDs and classes)
4. Clean DOM hierarchy
5. Initial state classes (hidden, active, disabled)

---

# Semantic Structure Rules

Always use semantic tags when appropriate.

Preferred tags: <header> <nav> <main> <section> <article> <aside> <footer>

Avoid excessive <div> usage when semantic alternatives exist.

---

# ID Rules (Critical)

IDs are the primary integration point with CSS and JavaScript.

Rules:

1. IDs must be unique
2. IDs must be stable
3. IDs must use kebab-case
4. IDs should represent function or content

Correct: user-profile, login-form, submit-button, sidebar-menu
Incorrect: btn1, box123, tempDiv

---

# Class Rules

Use classes for styling groups of elements.
Use IDs for unique components.

Class names must use kebab-case: btn, btn-primary, btn-secondary, btn-danger, note-card, note-card-header, modal-overlay

CRITICAL: Every class name used in HTML MUST also be styled in CSS.
Every class name that JS toggles MUST be defined in both HTML and CSS.

---

# Standard State Classes (Critical Cross-File Contract)

These are the ONLY state classes allowed across HTML, CSS, and JS.
Do NOT invent alternatives.

VISIBILITY:
- "hidden" — element is not visible
  HTML: add class="hidden" to elements that start hidden
  CSS: .hidden { display: none !important; }
  JS: el.classList.add("hidden") / el.classList.remove("hidden")

ACTIVE STATE:
- "active" — element is in active/selected state (tabs, nav items)
  CSS: styles the active appearance
  JS: el.classList.add("active") / el.classList.remove("active")

DISABLED STATE:
- "disabled" — element is non-interactive
  CSS: styles the disabled appearance
  JS: el.classList.add("disabled") / el.classList.remove("disabled")

BANNED alternatives (never use these):
- "is-open", "is-hidden", "is-visible", "show", "visible", "open", "closed"
- "is-active", "is-disabled", "is-selected"

---

# Initial State Classes (Critical)

Elements that should not be visible on page load MUST have class="hidden" in the HTML markup.

Correct:
<div id="add-modal" class="modal-overlay hidden">...</div>
<div id="edit-panel" class="panel hidden">...</div>

Incorrect (will flash on page load, and JS toggle won't work):
<div id="add-modal" class="modal-overlay">...</div>
<div id="edit-panel" class="panel">...</div>

---

# Persistent Action Buttons (Critical)

Every CRUD app needs a persistent "Add" / "Create" button that is ALWAYS visible,
regardless of app state. Do NOT put the only add button inside a welcome/empty-state message.

Correct pattern — always-visible add button:
<header class="app-header">
  <h1>My Notes</h1>
  <button id="add-note-btn" class="btn btn-primary">+ Add Note</button>
</header>

Incorrect — add button only inside welcome message (hidden when items exist):
<div id="welcome-message" class="hidden">
  <button id="start-adding-btn">Add First Note</button>  <!-- UNREACHABLE when hidden! -->
</div>

Rule: The primary "create/add" action MUST be in the header or a persistent toolbar,
not only inside a conditional/empty-state section.

---

# Modal / Overlay Pattern (Critical)

All modals MUST follow this exact structure:

<div id="my-modal" class="modal-overlay hidden">
  <div class="modal-content">
    <h2>Modal Title</h2>
    <form id="my-modal-form">
      <label for="my-field">Field</label>
      <input id="my-field" type="text">
      <div class="form-actions">
        <button type="submit" class="btn btn-primary">Save</button>
        <button type="button" id="my-modal-cancel" class="btn btn-secondary">Cancel</button>
      </div>
    </form>
  </div>
</div>

Rules:
- Outer div: class="modal-overlay hidden" (hidden by default)
- Inner div: class="modal-content"
- JS shows by removing "hidden", hides by adding "hidden"
- NEVER use "is-open" or other toggle classes for modals

---

# Dynamic Element Template (Critical)

When JS creates elements dynamically (cards, list items, etc.),
the HTML must define the class names those elements will use
so CSS knows what to style.

Add an HTML comment documenting the dynamic structure:

<!-- Dynamic note-card structure (created by JS):
<article class="note-card">
  <div class="note-card-header">
    <h3 class="note-card-title">Title</h3>
    <span class="note-card-date">Date</span>
  </div>
  <div class="note-card-body">
    <p>Content</p>
  </div>
  <div class="note-card-actions">
    <button class="btn btn-edit">Edit</button>
    <button class="btn btn-delete">Delete</button>
  </div>
</article>
-->

CRITICAL: The class names in this template MUST be the exact same
class names that JS uses when creating elements AND that CSS styles.

---

# Button Classes (Standard)

Always use these standard button classes:

- btn            — base button styling
- btn-primary    — primary action (save, create, submit)
- btn-secondary  — secondary action (cancel, close)
- btn-danger     — destructive action (delete)
- btn-edit       — edit action
- btn-delete     — alias for btn-danger on delete buttons

Example:
<button class="btn btn-primary">Save</button>
<button class="btn btn-secondary">Cancel</button>
<button class="btn btn-danger">Delete</button>

---

# Data Attributes

Use data-* attributes when JavaScript needs structured metadata.

Example:
<button id="delete-button" data-user-id="42">Delete</button>

---

# Accessibility Rules

Required practices:

- Use <label> for inputs with matching for="" attribute
- Use aria-* attributes when necessary
- Ensure buttons use <button> not <div>
- Interactive elements must be keyboard accessible

---

# Integration Contract

## With CSS (css.md)
- CSS must reference ONLY identifiers (IDs, classes) defined in this HTML
- CSS must define .hidden { display: none !important; }
- CSS must style ALL classes used in HTML, including dynamic element classes
- CSS must style the standard button classes (btn, btn-primary, etc.)

## With JavaScript (js.md)
- JS must reference elements using the EXACT IDs defined here
- JS must toggle ONLY the standard state classes: hidden, active, disabled
- JS must create dynamic elements using ONLY the class names documented here
- JS must NOT invent new class names

---

# Summary

HTML defines:
- Page structure and semantic meaning
- Element IDs (referenced by JS and CSS)
- Class names (styled by CSS, toggled by JS)
- Initial state (hidden elements start with class="hidden")
- Dynamic element templates (class names for JS-created elements)

All three files MUST agree on class names. HTML is the source of truth.