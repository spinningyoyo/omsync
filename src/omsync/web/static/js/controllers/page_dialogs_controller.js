// Dialogs for the Reconcile page and the Settings pages: opening one by id, closing them when the
// server asks, returning focus to the opener, and focusing the field a swap marks with
// data-autofocus. Regions reload themselves, so this controller does no fetching. A dialog that
// closes resets its form, so Cancel does not keep a half-typed entry.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  // Opens the dialog whose id the trigger names. The trigger gets focus back when it closes.
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

  // The server asks for this after a successful write, so no dialog stays open over a result.
  closeDialogs() {
    this.element.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  }

  // Runs on a dialog's close event, however it closed.
  restoreFocus(event) {
    event.currentTarget.querySelector("form")?.reset();
    const opener = this.opener;
    this.opener = null;
    if (opener?.isConnected) opener.focus();
  }

  // After a swap, focus the field the server marks with data-autofocus.
  settled(event) {
    const target = event.target;
    const field = target.matches?.("[data-autofocus]")
      ? target
      : target.querySelector?.("[data-autofocus]");
    field?.focus();
  }
}
