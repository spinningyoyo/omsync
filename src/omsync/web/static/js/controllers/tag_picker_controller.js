// A tag picker in a form that is not saved yet: the button names the ticked tags as they change.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["summary"];

  update() {
    const names = [...this.element.querySelectorAll("input[type=checkbox]:checked")].map((box) => box.dataset.name);
    this.summaryTarget.textContent = names.length ? names.join(", ") : "Tags";
  }
}
