// Keyboard navigation and inline editing for the register prototype.
//
// The server renders every row and fragment. This controller maps keys to htmx events
// (edit, save, cancel, open-split, close-split), and after each htmx swap it focuses the
// element the server marked with data-autofocus. Focus is lost whenever htmx replaces the
// focused element, so the server decides where focus goes next.

import { Application, Controller } from "/static/vendor/stimulus.js";

const FIELDS = ["payee", "category", "memo"];

// Validation errors come back as 422 fragments that must be swapped in.
htmx.config.responseHandling = [
  { code: "204", swap: false },
  { code: "[23]..", swap: true },
  { code: "422", swap: true, error: false },
  { code: "[45]..", swap: false, error: true },
];
// htmx waits 20 ms before settling by default, and focus is restored on settle. Settle in
// the same task as the swap so no frame paints with focus on <body>.
htmx.config.defaultSettleDelay = 0;

class RegisterController extends Controller {
  connect() {
    this.chain = null;
    this.focusAutofocus();
  }

  // Runs after every htmx swap inside the table.
  settled() {
    if (this.chain) {
      const { savedId, nextId } = this.chain;
      this.chain = null;
      const saved = document.getElementById(savedId);
      const next = document.getElementById(nextId);
      if (saved && next && !saved.querySelector("input")) {
        // Tab saved successfully, so open the next cell for editing.
        this.clearAutofocus();
        htmx.trigger(next, "edit");
        return;
      }
    }
    this.focusAutofocus();
  }

  keydown(event) {
    const { target, key } = event;

    const editor = target.closest("[data-split-editor]");
    if (editor) {
      if (key === "Escape") {
        event.preventDefault();
        htmx.trigger(editor.querySelector("[data-close-split]"), "close-split");
      }
      return;
    }

    const cell = target.closest("td[data-editable]");
    if (!cell) return;
    if (target.tagName === "INPUT") {
      this.keyWhileEditing(event, cell);
    } else if (target === cell) {
      this.keyOnCell(event, cell);
    }
  }

  // A display cell is focused.
  keyOnCell(event, cell) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    switch (event.key) {
      case "ArrowUp":
      case "ArrowDown":
        event.preventDefault();
        this.moveRow(cell, event.key === "ArrowDown" ? 1 : -1);
        break;
      case "ArrowLeft":
      case "ArrowRight":
        event.preventDefault();
        this.moveColumn(cell, event.key === "ArrowRight" ? 1 : -1);
        break;
      case "Enter":
        event.preventDefault();
        htmx.trigger(cell, "edit");
        break;
      case "s":
      case "S":
        event.preventDefault();
        this.openSplit(cell);
        break;
    }
  }

  // An input inside an editable cell is focused.
  keyWhileEditing(event, cell) {
    if (event.isComposing) return; // Enter confirms an IME composition; it does not save.
    switch (event.key) {
      case "Enter":
        event.preventDefault();
        htmx.trigger(cell.querySelector("input"), "save");
        break;
      case "Escape":
        event.preventDefault();
        htmx.trigger(cell, "cancel");
        break;
      case "Tab":
        event.preventDefault();
        this.saveAndMove(cell, event.shiftKey ? -1 : 1);
        break;
    }
  }

  saveAndMove(cell, step) {
    const next = this.neighbour(cell, step);
    this.chain = next ? { savedId: cell.id, nextId: next.id } : null;
    htmx.trigger(cell.querySelector("input"), "save");
  }

  neighbour(cell, step) {
    const cells = [...this.element.querySelectorAll("td[data-editable]")];
    return cells[cells.indexOf(cell) + step] ?? null;
  }

  moveRow(cell, step) {
    const groups = [...this.element.querySelectorAll("tbody[data-txn-group]")];
    const index = groups.findIndex((group) => group.dataset.txnId === cell.dataset.txnId);
    const group = index < 0 ? undefined : groups[index + step];
    if (group) this.focusCell(group.dataset.txnId, cell.dataset.field);
  }

  moveColumn(cell, step) {
    const index = FIELDS.indexOf(cell.dataset.field) + step;
    if (index >= 0 && index < FIELDS.length) {
      this.focusCell(cell.dataset.txnId, FIELDS[index]);
    }
  }

  focusCell(txnId, field) {
    document.getElementById(`cell-${txnId}-${field}`)?.focus();
  }

  openSplit(cell) {
    const txnId = cell.dataset.txnId;
    const open = document.getElementById(`split-editor-${txnId}`);
    if (open) {
      // Already open: keep what was typed and return to it.
      open.querySelector("input[name=amount]")?.focus();
      return;
    }
    htmx.trigger(document.getElementById(`split-btn-${txnId}`), "open-split");
  }

  // Clicking a non-editable cell focuses that row's payee. Clicks inside inputs and
  // buttons keep their own behaviour.
  click(event) {
    const { target } = event;
    if (target.closest("[data-split-editor], button, input")) return;
    const group = target.closest("tbody[data-txn-group]");
    if (!group) return;
    const cell =
      target.closest("td[data-editable]") ?? group.querySelector("td[data-field='payee']");
    (cell.querySelector("input") ?? cell).focus();
  }

  focusAutofocus() {
    const target = this.element.querySelector("[data-autofocus]");
    if (!target) return;
    target.removeAttribute("data-autofocus");
    target.focus();
    if (target.tagName === "INPUT") target.select();
  }

  clearAutofocus() {
    this.element
      .querySelectorAll("[data-autofocus]")
      .forEach((el) => el.removeAttribute("data-autofocus"));
  }
}

const application = Application.start();
application.register("register", RegisterController);
