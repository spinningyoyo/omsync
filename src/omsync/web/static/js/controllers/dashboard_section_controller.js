// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/dashboard_section_controller.js.
// Changes: the collapsed state is written to the section's data-collapsed attribute, and a change is
// announced as dashboard-layout-changed, so dashboard_sortable saves the whole layout in one request.
// Sure saves each toggle on its own. The button is a native button, so Enter and Space already click it.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["content", "chevron", "button"];
  static values = { collapsed: Boolean };

  connect() {
    this.render();
  }

  toggle(event) {
    event.preventDefault();
    this.collapsedValue = !this.collapsedValue;
    this.render();
    this.element.dispatchEvent(new CustomEvent("dashboard-layout-changed", { bubbles: true }));
  }

  render() {
    const collapsed = this.collapsedValue;
    this.contentTarget.hidden = collapsed;
    this.chevronTarget.classList.toggle("-rotate-90", collapsed);
    this.buttonTarget.setAttribute("aria-expanded", String(!collapsed));
    this.element.dataset.collapsed = String(collapsed);
  }
}
