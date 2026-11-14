// The filter bar's dialogs and condition form. The chips, the add menu, the all/any toggle, the
// search box, and the saved filters are htmx requests. This controller only opens and closes the
// dialogs, shows the value inputs that the chosen operation needs, and closes the condition form,
// which opens as a popover in the slot of the chip or the add menu that asked for it.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  openDialog(event) {
    const dialog = document.getElementById(event.currentTarget.dataset.dialogId);
    if (!dialog || dialog.open) return;
    this.opener = event.currentTarget;
    dialog.showModal();
    dialog.querySelector("[data-autofocus]")?.focus();
  }

  closeDialog(event) {
    event.currentTarget.closest("dialog")?.close();
  }

  // Runs when a dialog closes, however it closed, so focus goes back to what opened it.
  restoreFocus() {
    const opener = this.opener;
    this.opener = null;
    if (opener?.isConnected) opener.focus();
  }

  // Show the value inputs for the chosen operation, and hide the rest.
  opChanged(event) {
    const op = event.target.value;
    event.target
      .closest("form")
      .querySelectorAll("[data-ops]")
      .forEach((el) => {
        el.hidden = !el.dataset.ops.split(" ").includes(op);
      });
  }

  // Closes the form in a slot, and gives focus back to the trigger that opened it.
  closeForm(slot) {
    slot.replaceChildren();
    slot.closest("[data-filter-anchor]").querySelector("button").focus();
  }

  cancelForm(event) {
    this.closeForm(event.currentTarget.closest(".filter-slot"));
  }

  formKeydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      this.closeForm(event.currentTarget.closest(".filter-slot"));
    }
  }

  // A click outside a trigger and its open form closes the form. Opening another trigger's form
  // is such a click, so only one form is open at a time.
  clickOutside(event) {
    this.element.querySelectorAll(".filter-slot:not(:empty)").forEach((slot) => {
      if (!slot.closest("[data-filter-anchor]").contains(event.target)) slot.replaceChildren();
    });
  }
}
