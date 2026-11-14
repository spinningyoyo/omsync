// The bulk edit's selection: checkboxes in the register table, shift-click ranges, the header
// checkbox, "select all matching", the action bar, and the confirm dialog. While rows are selected
// the action bar takes the place of the filter bar (the toolbar target).
//
// Each checkbox belongs to #bulk-form through its form attribute, so the form's values carry the
// selected IDs to the bulk requests. The server re-renders the form after every change. A reply that
// re-renders the table fires register-reset, which clears the selection, because the rows it named
// were replaced. Any other swap keeps the selection: the checkboxes are set from it again.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["toolbar", "bar", "count", "selectAllWrap", "selectAllInput", "actionInput", "dialog", "all"];

  connect() {
    this.selected = new Set();
    this.selectAll = false;
    this.anchor = null;
    this.opener = null;
    this.sync();
  }

  get form() {
    return this.element.querySelector("#bulk-form");
  }

  get boxes() {
    return [...this.element.querySelectorAll('input[name="ids"]')];
  }

  // Rows

  clickRow(event) {
    const box = event.currentTarget;
    if (event.shiftKey && this.anchor?.isConnected && this.anchor !== box) {
      const boxes = this.boxes;
      const from = boxes.indexOf(this.anchor);
      const to = boxes.indexOf(box);
      const [low, high] = from < to ? [from, to] : [to, from];
      boxes.slice(low, high + 1).forEach((other) => {
        other.checked = box.checked;
      });
    }
    this.anchor = box;
    this.selectAll = false;
    this.fromBoxes();
  }

  toggleAll(event) {
    const on = event.currentTarget.checked;
    this.boxes.forEach((box) => {
      box.checked = on;
    });
    this.selectAll = false;
    this.fromBoxes();
  }

  // Every matching transaction, not only the rows on screen. The server expands the filter.
  selectAllMatching() {
    this.selectAll = true;
    this.boxes.forEach((box) => {
      box.checked = true;
    });
    this.sync();
  }

  // Bulk actions

  act(event) {
    const action = event.currentTarget.dataset.bulkAction;
    if (action === "clear_selection") {
      this.reset();
      return;
    }
    if (!this.form) return;
    this.opener = event.currentTarget;
    this.actionInputTarget.value = action;
    const url = action === "clear_category" ? "/transactions/bulk/preview" : "/transactions/bulk/form";
    this.request(url);
  }

  // The category or memo step, or the confirm step: every submit of the bulk form is one of them.
  submit(event) {
    event.preventDefault();
    this.request("/transactions/bulk/preview");
  }

  request(url) {
    htmx.ajax("POST", url, { source: this.form, target: "#bulk-dialog-body", swap: "innerHTML" });
  }

  closeDialog() {
    if (this.hasDialogTarget && this.dialogTarget.open) this.dialogTarget.close();
  }

  // Runs when the bulk dialog closes. Focus goes back to its button, or to the table when that button
  // was replaced by the change that closed it.
  restoreFocus() {
    const opener = this.opener;
    this.opener = null;
    const fallback = this.element.querySelector(".table-scroll");
    (opener?.isConnected ? opener : fallback)?.focus();
  }

  // Events from the server. A table or form re-render clears the selection. It never moves focus,
  // because a search reply re-renders the table while the user is typing in the search box.
  reset() {
    this.selected = new Set();
    this.selectAll = false;
    this.anchor = null;
    this.boxes.forEach((box) => {
      box.checked = false;
    });
    this.closeDialog();
    this.sync();
  }

  // A bulk change is done. The event arrives before the reply's out-of-band swap, which replaces the
  // button that opened the dialog, so focus moves to the table after the swap, not before it.
  bulkDone() {
    this.opener = null;
    this.reset();
    setTimeout(() => this.element.querySelector(".table-scroll")?.focus(), 0);
  }

  // After each swap: keep checked rows checked, and open the bulk dialog when its body was filled.
  settled(event) {
    const target = event.detail?.target;
    this.boxes.forEach((box) => {
      box.checked = this.selected.has(box.value) || this.selectAll;
    });
    this.sync();
    if (target?.id === "bulk-dialog-body" && target.childElementCount && this.hasDialogTarget) {
      if (!this.dialogTarget.open) {
        this.dialogTarget.showModal();
        this.dialogTarget.querySelector("[data-autofocus]")?.focus();
      }
    }
  }

  // State

  fromBoxes() {
    this.selected = new Set(this.boxes.filter((box) => box.checked).map((box) => box.value));
    this.sync();
  }

  sync() {
    const boxes = this.boxes;
    const checked = boxes.filter((box) => box.checked).length;
    const total = Number(this.hasBarTarget ? this.barTarget.dataset.total : 0);
    const selecting = (this.selectAll ? total : checked) > 0;
    if (this.hasBarTarget) this.barTarget.hidden = !selecting;
    if (this.hasToolbarTarget) this.toolbarTarget.hidden = selecting;
    if (this.hasCountTarget) {
      this.countTarget.textContent = this.selectAll ? `All ${total} matching selected` : `${checked} selected`;
    }
    if (this.hasSelectAllInputTarget) this.selectAllInputTarget.value = this.selectAll ? "1" : "0";
    const every = boxes.length > 0 && checked === boxes.length;
    if (this.hasAllTarget) {
      this.allTarget.checked = every;
      this.allTarget.indeterminate = checked > 0 && !every;
    }
    if (this.hasSelectAllWrapTarget) {
      this.selectAllWrapTarget.hidden = !(every && !this.selectAll && total > boxes.length);
    }
  }
}
