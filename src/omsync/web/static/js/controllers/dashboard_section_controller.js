// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/dashboard_section_controller.js.
// Changes: the collapsed state is written to the section's data-collapsed attribute, and a change is
// announced as dashboard-layout-changed, so dashboard_sortable saves the whole layout in one request.
// Sure saves each toggle on its own. The button is a native button, so Enter and Space already click it.
// A section with grouped rows also has Sure's Expand all / Collapse all button, which opens or closes
// every <details> group in the body. It is not saved: groups open as the page draws them.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["content", "chevron", "button", "groupsButton", "expandIcon", "collapseIcon"];
  static values = { collapsed: Boolean };

  connect() {
    this.render();
    if (this.hasGroupsButtonTarget) {
      // A group opened or closed by hand changes what the button offers. toggle does not bubble.
      this.syncGroups = this.syncGroups.bind(this);
      this.contentTarget.addEventListener("toggle", this.syncGroups, true);
      this.syncGroups();
    }
  }

  disconnect() {
    if (this.hasGroupsButtonTarget) this.contentTarget.removeEventListener("toggle", this.syncGroups, true);
  }

  get groups() {
    return Array.from(this.contentTarget.querySelectorAll("details"));
  }

  toggleGroups() {
    const open = this.groups.some((group) => !group.open);
    for (const group of this.groups) group.open = open;
    this.syncGroups();
  }

  // Expand all while any group is closed, Collapse all once every group is open.
  syncGroups() {
    const allOpen = this.groups.length > 0 && this.groups.every((group) => group.open);
    const label = allOpen ? "Collapse all" : "Expand all";
    this.groupsButtonTarget.setAttribute("aria-label", label);
    this.groupsButtonTarget.dataset.tip = label;
    this.expandIconTarget.hidden = allOpen;
    this.collapseIconTarget.hidden = !allOpen;
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
