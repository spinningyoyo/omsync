// The Categories page: its dialogs, the table's selection, focus after each swap, clearing a triage
// form, and showing the keyword row a conflict link names. Regions reload themselves on the
// categories-changed event, so this controller does no fetching.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["bar", "count", "all"];

  // Landing from a conflict link, the browser has scrolled to the row; this marks it.
  connect() {
    this.flash(location.hash);
  }

  get boxes() {
    return [...this.element.querySelectorAll('#category-list input[name="ids"]')];
  }

  // A ticked row is not a change to the row, so its change event stops here, short of the row's save.
  selectionChanged(event) {
    event.stopPropagation();
    this.syncSelection();
  }

  toggleAll(event) {
    event.stopPropagation();
    this.boxes.forEach((box) => {
      box.checked = event.currentTarget.checked;
    });
    this.syncSelection();
  }

  // The bar shows while any row is ticked. A reload of the table clears the ticks and the bar.
  syncSelection() {
    const boxes = this.boxes;
    const ticked = boxes.filter((box) => box.checked).length;
    if (this.hasBarTarget) this.barTarget.hidden = ticked === 0;
    if (this.hasCountTarget) this.countTarget.textContent = `${ticked} selected`;
    if (this.hasAllTarget) {
      this.allTarget.checked = boxes.length > 0 && ticked === boxes.length;
      this.allTarget.indeterminate = ticked > 0 && ticked < boxes.length;
    }
  }

  // Opens a dialog by id. The button that opened it gets focus back when the dialog closes.
  openDialog(event) {
    const dialog = document.getElementById(event.currentTarget.dataset.dialogId);
    if (!dialog || dialog.open) return;
    this.opener = event.currentTarget;
    this.openerId = event.currentTarget.id;
    dialog.showModal();
    dialog.querySelector("[data-autofocus]")?.focus();
  }

  closeDialog(event) {
    event.currentTarget.closest("dialog")?.close();
  }

  // The server asks for this after a successful write, so a dialog never stays open over a result.
  closeDialogs() {
    this.element.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  }

  // A saved change redraws the table, so the opener may be gone; its replacement has the same id.
  restoreFocus() {
    const opener = this.opener;
    const id = this.openerId;
    this.opener = null;
    this.openerId = null;
    const target = opener?.isConnected ? opener : id && document.getElementById(id);
    target?.focus();
  }

  // Empties the triage slot that holds an open keyword form.
  clearSlot(event) {
    event.currentTarget.closest("[data-slot]")?.replaceChildren();
  }

  // A conflict link names a keyword row. When that row is on the page, it is shown here, so the link
  // works from any address; otherwise the link loads the page with the row's list.
  reveal(event) {
    const hash = new URL(event.currentTarget.href).hash;
    const row = document.getElementById(hash.slice(1));
    if (!row) return;
    event.preventDefault();
    row.scrollIntoView({ block: "nearest" });
    this.flash(hash);
  }

  // Highlights the row once. The class comes off when the animation ends, so a second visit replays it.
  flash(hash) {
    const row = hash && document.getElementById(hash.slice(1));
    if (!row) return;
    row.classList.remove("row-flash");
    void row.offsetWidth;
    row.classList.add("row-flash");
    row.addEventListener("animationend", () => row.classList.remove("row-flash"), { once: true });
  }

  // After a swap, focus the field the server marks with data-autofocus (a refused form's field).
  settled(event) {
    const target = event.target;
    const field = target.matches?.("[data-autofocus]")
      ? target
      : target.querySelector?.("[data-autofocus]");
    field?.focus();
  }
}
