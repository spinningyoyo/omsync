// The wizard's provider key follows the institution key until the person types their own. A key they
// typed is never replaced: the controller changes the provider field only while it still holds the
// last suggestion, or is empty.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["institution", "provider"];

  connect() {
    this.suggestion = this.institutionTarget.value;
  }

  institutionChanged() {
    const next = this.institutionTarget.value;
    if (this.providerTarget.value === "" || this.providerTarget.value === this.suggestion) {
      this.providerTarget.value = next;
    }
    this.suggestion = next;
  }
}
