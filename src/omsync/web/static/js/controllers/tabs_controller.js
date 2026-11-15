// Adapted from Sure (we-promise/sure), AGPL-3.0: app/components/DS/tabs_controller.js
// Changes: the active tab is the controller's value, with no session or URL persistence. An
// optional field target holds the active id for a request that reloads the tabs, and an optional
// urlParam names the query parameter that keeps it in the address and in links to this page.
import { Controller } from "/static/vendor/stimulus.js";

// WAI-ARIA tabs with manual activation: arrow keys move focus, Enter or Space selects.
export default class extends Controller {
  static targets = ["tab", "panel", "field"];
  static values = { active: String, urlParam: String };
  static classes = ["active", "inactive"];

  activeValueChanged() {
    this.render();
  }

  select(event) {
    this.activeValue = event.currentTarget.dataset.id;
    this.remember();
  }

  remember() {
    const name = this.urlParamValue;
    if (!name) return;
    const here = new URL(window.location.href);
    here.searchParams.set(name, this.activeValue);
    window.history.replaceState(window.history.state, "", here);
    for (const link of document.querySelectorAll("a[href]")) {
      const target = new URL(link.href, here);
      if (target.pathname !== here.pathname || !target.searchParams.has(name)) continue;
      target.searchParams.set(name, this.activeValue);
      link.setAttribute("href", target.pathname + target.search);
    }
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
    if (this.hasFieldTarget) this.fieldTarget.value = this.activeValue;
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
