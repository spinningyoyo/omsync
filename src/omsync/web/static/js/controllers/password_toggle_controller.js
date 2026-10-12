// Show/hide button for a password field (pages/sign_in.html). The field starts hidden and returns
// to hidden whenever the page reloads, so a failed sign-in never leaves the password visible.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["input", "button", "show", "hide"];

  toggle() {
    const reveal = this.inputTarget.type === "password";
    this.inputTarget.type = reveal ? "text" : "password";
    this.buttonTarget.setAttribute("aria-pressed", String(reveal));
    this.buttonTarget.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
    this.showTarget.hidden = reveal;
    this.hideTarget.hidden = !reveal;
  }
}
