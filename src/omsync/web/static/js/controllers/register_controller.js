// Keyboard navigation and inline editing for the transactions register.
//
// Category is a select, always visible, as the Categories page's name and type are. A change saves
// the cell: Tab moves on, a click leaves it, or Enter is pressed. Enter
// also moves down to the same field of the next row. The server renders every row and fragment. One
// delegated controller sits on the register's wrapper and builds each request from data-txn-id and
// data-field, so cells carry no hx-* attributes. After each htmx swap it focuses the element the
// server marks with data-autofocus, because a swap removes whatever was focused.
import { Controller } from "/static/vendor/stimulus.js";

const PREVIEW_DELAY_MS = 150;

export default class extends Controller {
  connect() {
    this.timer = null;
    this.keyChoice = false;
    this.unsaved = null;
    this.focusAutofocus();
  }

  disconnect() {
    clearTimeout(this.timer);
  }

  // Keys

  keydown(event) {
    const { target, key } = event;

    const editor = target.closest("[data-split-editor]");
    if (editor) {
      if (key === "Escape") {
        event.preventDefault();
        this.closeSplit(editor.dataset.txnId);
      }
      return;
    }

    const cell = target.closest("td[data-editable]");
    if (cell && (target.tagName === "INPUT" || target.tagName === "SELECT")) {
      this.keyInCell(event, cell);
    }
  }

  // A cell's field has focus. Printable keys are text, or type-ahead in a select; only the keys
  // below do more.
  keyInCell(event, cell) {
    if (event.isComposing) return; // Enter confirms an IME composition; it does not save.
    const input = event.target;
    if (input.tagName === "SELECT") {
      this.keyChoice = true;
      // An open list belongs to the browser, so its arrows and Enter choose an option.
      if (event.key !== "Escape" && this.listOpen(input)) return;
    }
    switch (event.key) {
      case "Enter":
        event.preventDefault();
        // Moving to the next row blurs this field, and the blur saves it.
        if (!this.moveRow(cell, 1)) input.blur();
        break;
      case "ArrowUp":
      case "ArrowDown":
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        event.preventDefault();
        this.moveRow(cell, event.key === "ArrowDown" ? 1 : -1);
        break;
      case "Escape":
        event.preventDefault();
        this.unsaved = null;
        this.restore(input);
        input.blur();
        break;
    }
  }

  // Whether a select's list of options is showing. A browser without :open is taken to be closed,
  // so its arrows move between rows.
  listOpen(select) {
    return CSS.supports("selector(:open)") && select.matches(":open");
  }

  // Puts back the value the server last rendered.
  restore(field) {
    if (field.tagName === "SELECT") {
      const saved = [...field.options].findIndex((option) => option.defaultSelected);
      field.selectedIndex = Math.max(0, saved);
    } else {
      field.value = field.defaultValue;
    }
  }

  // Focuses the same field of the row above or below. Returns false at the first or last row.
  moveRow(cell, step) {
    const groups = [...this.element.querySelectorAll("tbody[data-txn-group]")];
    const index = groups.findIndex((group) => group.dataset.txnId === cell.dataset.txnId);
    const group = index < 0 ? undefined : groups[index + step];
    const input = group && this.fieldInput(group.dataset.txnId, cell.dataset.field);
    input?.focus();
    return Boolean(input);
  }

  // Clicking anywhere in a row focuses the field under the pointer, or the category elsewhere.
  // Clicks inside buttons, fields, links, and the split editor keep their own behaviour.
  click(event) {
    const { target } = event;
    if (target.closest("[data-split-editor], button, input, select, a")) return;
    const group = target.closest("tbody[data-txn-group]");
    if (!group) return;
    const field = target.closest("td[data-editable]")?.dataset.field ?? "category";
    this.fieldInput(group.dataset.txnId, field)?.focus();
  }

  // Forms: split forms are handled here, so none of them navigates.
  submit(event) {
    const form = event.target;
    const kind = form.dataset.registerForm;
    if (!kind) return;
    event.preventDefault();
    if (kind === "split") this.saveSplit(form);
    else if (kind === "clear") this.removeSplit(form);
  }

  // A changed cell saves. Other changes in the register, such as the bulk checkboxes, pass through.
  //
  // A closed select jumps to an option on each type-ahead key and fires change every time. Saving
  // each jump would make one undo batch per keystroke, and each swap would take focus from the
  // select. So a choice made with the keyboard waits until the field is left, by Tab, Enter, or a
  // click elsewhere. A choice made with the pointer is whole when change fires, so it saves then.
  change(event) {
    const cell = event.target.closest("td[data-editable]");
    if (!cell) return;
    if (event.target.tagName === "SELECT" && this.keyChoice) this.unsaved = cell;
    else this.saveCell(cell);
  }

  // The pointer picks the next option of a select, so its change saves at once.
  pointer(event) {
    if (event.target.tagName === "SELECT") this.keyChoice = false;
  }

  // Leaving a select that holds a keyboard choice saves it.
  leave(event) {
    const cell = this.unsaved;
    if (!cell?.contains(event.target)) return;
    this.unsaved = null;
    this.saveCell(cell);
  }

  // Typing in the split editor asks the server for the amount left, after a short pause.
  input(event) {
    const form = event.target.closest("[data-register-form='split']");
    if (!form) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.previewSplit(form), PREVIEW_DELAY_MS);
  }

  // Runs after every htmx swap inside the register, including the toast region.
  settled() {
    this.focusAutofocus();
  }

  // Cells: each request names the row and field. The key and version are the ones the server
  // rendered on the cell.

  url(path) {
    const account = this.element.dataset.account;
    if (!account) return path;
    return `${path}${path.includes("?") ? "&" : "?"}account=${encodeURIComponent(account)}`;
  }

  async saveCell(cell) {
    // A pointer choice after a keyboard one saves the cell now, so leaving it must not save again.
    if (this.unsaved === cell) this.unsaved = null;
    const { txnId, field } = cell.dataset;
    const input = this.fieldInput(txnId, field);
    const values = {
      value: input.value,
      idempotency_key: cell.dataset.key,
      version: cell.dataset.version,
    };
    // A field that still has focus when it saves, as a select does after a pointer choice, loses
    // it to the swap, so it takes focus back.
    const focused = document.activeElement === input;
    await htmx.ajax("POST", this.url(`/transactions/${txnId}/cell/${field}`), {
      values,
      target: cell,
      swap: "outerHTML",
    });
    if (focused && document.activeElement === document.body) this.fieldInput(txnId, field)?.focus();
  }

  // Split editor

  openSplitFromButton(event) {
    this.openSplit(event.currentTarget.dataset.txnId);
  }

  closeSplitFromButton(event) {
    this.closeSplit(event.currentTarget.dataset.txnId);
  }

  addLine(event) {
    const txnId = event.currentTarget.dataset.txnId;
    const lines = this.editorFor(txnId)?.querySelector("[data-split-lines]");
    if (lines) {
      htmx.ajax("GET", this.url(`/transactions/${txnId}/split/line`), {
        target: lines,
        swap: "beforeend",
      });
    }
  }

  openSplit(txnId) {
    const open = this.editorFor(txnId);
    if (open) {
      // Already open: keep what was typed and return to it.
      open.querySelector("input[name=amount]")?.focus();
      return;
    }
    htmx.ajax("GET", this.url(`/transactions/${txnId}/split`), {
      target: this.group(txnId),
      swap: "outerHTML",
    });
  }

  closeSplit(txnId) {
    htmx.ajax("GET", this.url(`/transactions/${txnId}/split/close`), {
      target: this.group(txnId),
      swap: "outerHTML",
    });
  }

  previewSplit(form) {
    const txnId = form.closest("[data-split-editor]").dataset.txnId;
    htmx.ajax("POST", this.url(`/transactions/${txnId}/split/preview`), {
      source: form,
      target: `#amount-left-${txnId}`,
      swap: "outerHTML",
    });
  }

  saveSplit(form) {
    const group = form.closest("tbody[data-txn-group]");
    const txnId = group.dataset.txnId;
    htmx.ajax("POST", this.url(`/transactions/${txnId}/split`), {
      source: form,
      target: group,
      swap: "outerHTML",
    });
  }

  removeSplit(form) {
    const group = form.closest("tbody[data-txn-group]");
    const txnId = group.dataset.txnId;
    htmx.ajax("POST", this.url(`/transactions/${txnId}/split/clear`), {
      source: form,
      target: group,
      swap: "outerHTML",
    });
  }

  // Lookups

  cell(txnId, field) {
    return this.element.querySelector(`td[data-txn-id="${txnId}"][data-field="${field}"]`);
  }

  fieldInput(txnId, field) {
    return this.cell(txnId, field)?.querySelector('[name="value"]');
  }

  group(txnId) {
    return this.element.querySelector(`tbody[data-txn-group][data-txn-id="${txnId}"]`);
  }

  editorFor(txnId) {
    return this.element.querySelector(`tr[data-split-editor][data-txn-id="${txnId}"]`);
  }

  // Focus

  focusAutofocus() {
    const target = this.element.querySelector("[data-autofocus]");
    if (!target) return;
    target.removeAttribute("data-autofocus");
    target.focus();
    if (target.tagName === "INPUT") target.select();
  }
}
