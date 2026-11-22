// Sends a one-time code as soon as the field holds a whole code, so the person need not press Send
// (the MFA question on the Data sync page, templates/settings/sync_parts.html).
//
// The bank's connector says how many digits its code has, and the form carries that as the length.
// A code is digits only; spaces and dashes pasted with it are dropped. Any other entry waits for
// the Send button, and a question with no known length has no controller at all.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["input"];
  static values = { length: Number };

  connect() {
    this.waiting = false;
  }

  check() {
    const typed = this.inputTarget.value;
    if (this.waiting || !/^[\d\s-]+$/.test(typed)) return;
    if (typed.replace(/\D/g, "").length === this.lengthValue) this.send();
  }

  send() {
    this.inputTarget.value = this.inputTarget.value.replace(/\D/g, "");
    this.waiting = true;
    this.element.requestSubmit();
  }

  // The reply is in: a refused answer can be corrected and sent again.
  done() {
    this.waiting = false;
  }
}
