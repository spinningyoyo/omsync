// Adapted from Sure (we-promise/sure), AGPL-3.0: app/components/DS/tabs_controller.js
// Changes: the active tab is the controller's value, with no session or URL persistence.
import { Controller } from "/static/vendor/stimulus.js";

// WAI-ARIA tabs with manual activation: arrow keys move focus, Enter or Space selects.
export default class extends Controller {
  static targets = ["tab", "panel"];
  static values = { active: String };
  static classes = ["active", "inactive"];

  activeValueChanged() {
    this.render();
  }

  select(event) {
    this.activeValue = event.currentTarget.dataset.id;
  }

  keydown(event) {
    const tabs = this.tabTargets;
    const index = tabs.indexOf(event.currentTarget);
    if (index < 0) return;

    let next;
    switch (event.key) {
      case "ArrowRight":
        next = (index + 1) % tabs.length;
        break;
      case "ArrowLeft":
        next = (index - 1 + tabs.length) % tabs.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = tabs.length - 1;
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        this.select(event);
        return;
      default:
        return;
    }
    event.preventDefault();
    tabs[next].focus();
  }

  render() {
    this.tabTargets.forEach((tab) => {
      const selected = tab.dataset.id === this.activeValue;
      tab.setAttribute("aria-selected", String(selected));
      tab.setAttribute("tabindex", selected ? "0" : "-1");
      tab.classList.remove(...(selected ? this.inactiveClasses : this.activeClasses));
      tab.classList.add(...(selected ? this.activeClasses : this.inactiveClasses));
    });
    this.panelTargets.forEach((panel) => {
      panel.classList.toggle("hidden", panel.dataset.id !== this.activeValue);
    });
  }
}
