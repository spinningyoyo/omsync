// Adapted from Sure (we-promise/sure), AGPL-3.0: app/components/DS/dialog_controller.js
// Changes: the trigger and the native <dialog> share one controller, and there are no Turbo hooks.
import { Controller } from "/static/vendor/stimulus.js";

// Native <dialog>: showModal() traps Tab, puts focus inside and closes on Escape.
export default class extends Controller {
  static targets = ["dialog", "content"];

  open(event) {
    this.opener = event.currentTarget;
    this.dialogTarget.showModal();
  }

  close() {
    this.dialogTarget.close();
  }

  // A click outside the panel (the backdrop area) closes the dialog.
  clickOutside(event) {
    if (!this.contentTarget.contains(event.target)) this.close();
  }

  // Runs on the dialog's close event, however it closed, so focus goes back to the trigger. A
  // trigger inside a menu is hidden by then, since the menu closes as the item is chosen, so focus
  // goes to the menu's button instead.
  restoreFocus() {
    const menuButton = this.opener?.closest("[data-controller~='menu']")?.querySelector("[data-menu-target='button']");
    const target = menuButton ?? this.opener;
    if (target?.isConnected) target.focus();
    this.opener = null;
  }
}
