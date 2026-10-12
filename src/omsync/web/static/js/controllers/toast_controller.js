// Removes an undo toast after 8 seconds. A newer change replaces the toast before then, and
// the new toast starts its own timer when it connects.
import { Controller } from "/static/vendor/stimulus.js";

const HIDE_AFTER_MS = 8000;

export default class extends Controller {
  connect() {
    this.timer = setTimeout(() => this.element.remove(), HIDE_AFTER_MS);
  }

  disconnect() {
    clearTimeout(this.timer);
  }
}
