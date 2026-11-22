// The Tags page: its dialog, and the badge that previews a tag as its name and color are typed.
// The list reloads itself on the tags-changed event, so this controller does no fetching.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  // Opens the dialog the button names. Its body is filled by the button's own request.
  openDialog(event) {
    const dialog = document.getElementById(event.currentTarget.dataset.dialogId);
    if (!dialog || dialog.open) return;
    this.openerId = event.currentTarget.id;
    dialog.showModal();
  }

  closeDialog(event) {
    event.currentTarget.closest("dialog")?.close();
  }

  // The server asks for this after a successful write, so a dialog never stays open over a result.
  closeDialogs() {
    this.element.querySelectorAll("dialog[open]").forEach((dialog) => {
      dialog.close();
    });
  }

  restoreFocus() {
    const id = this.openerId;
    this.openerId = null;
    if (id) document.getElementById(id)?.focus();
  }

  // After a swap, focus the field the server marks with data-autofocus.
  settled(event) {
    const target = event.target;
    const field = target.matches?.("[data-autofocus]") ? target : target.querySelector?.("[data-autofocus]");
    field?.focus();
  }

  // The preview badge follows the name and the chosen color.
  preview(event) {
    const form = event.currentTarget.closest("form");
    const badge = form.querySelector("[data-tag-badge]");
    const name = form.querySelector('input[name="name"]').value.trim();
    const color = form.querySelector('input[name="color"]:checked')?.value;
    badge.querySelector("[data-tag-badge-name]").textContent = name || badge.dataset.placeholder;
    if (color) {
      badge.style.backgroundColor = `${color}18`;
      badge.style.color = color;
      badge.style.borderColor = `${color}38`;
      badge.querySelector("[data-tag-badge-dot]").style.backgroundColor = color;
    }
  }
}
