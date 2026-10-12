// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/theme_controller.js
// and app/views/layouts/_dark_mode_check.html.erb. Changes: a three-way choice (light, dark,
// system) saved in localStorage under "omsync-theme", and no server-side preference.
import { Controller } from "/static/vendor/stimulus.js";

const STORAGE_KEY = "omsync-theme";
const CHOICES = ["light", "dark", "system"];
const DARK_QUERY = "(prefers-color-scheme: dark)";

function storedChoice() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return CHOICES.includes(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

function storeChoice(choice) {
  try {
    localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // Storage is blocked (private window, site data cleared): the choice lasts for this page.
  }
}

function resolve(choice) {
  if (choice === "system") {
    return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
  }
  return choice;
}

// Three-way switch. Each option carries data-theme-choice and data-theme-target="option".
export default class extends Controller {
  static targets = ["option"];

  connect() {
    this.choice = storedChoice();
    this.media = window.matchMedia(DARK_QUERY);
    this.onSystemChange = () => {
      if (this.choice === "system") this.apply();
    };
    this.media.addEventListener("change", this.onSystemChange);
    this.apply();
    this.render();
  }

  disconnect() {
    this.media.removeEventListener("change", this.onSystemChange);
  }

  choose(event) {
    this.choice = event.currentTarget.dataset.themeChoice;
    storeChoice(this.choice);
    this.apply();
    this.render();
  }

  apply() {
    document.documentElement.setAttribute("data-theme", resolve(this.choice));
  }

  render() {
    this.optionTargets.forEach((option) => {
      const selected = option.dataset.themeChoice === this.choice;
      option.setAttribute("aria-pressed", String(selected));
      option.classList.toggle("segmented-control__segment--active", selected);
    });
  }
}
