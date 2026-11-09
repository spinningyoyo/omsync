// Adapted from Sure (we-promise/sure), AGPL-3.0: app/components/DS/menu_controller.js, for a panel of
// controls rather than a list of actions: there is no arrow-key movement, since the panel's buttons
// are reached with Tab, and a click inside the panel leaves it open so several choices can be made.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["button", "panel"];

  get isOpen() {
    return !this.panelTarget.hidden;
  }

  toggle() {
    this.setOpen(!this.isOpen);
  }

  setOpen(open) {
    this.panelTarget.hidden = !open;
    this.buttonTarget.setAttribute("aria-expanded", String(open));
  }

  clickOutside(event) {
    if (this.isOpen && !this.element.contains(event.target)) this.setOpen(false);
  }

  keydown(event) {
    if (event.key !== "Escape" || !this.isOpen) return;
    event.preventDefault();
    event.stopPropagation();
    this.setOpen(false);
    this.buttonTarget.focus();
  }
}
