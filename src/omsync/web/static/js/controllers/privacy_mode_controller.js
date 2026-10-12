// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/privacy_mode_controller.js.
// Changes: the state is kept in localStorage under "omsync-privacy-mode" with every access guarded
// by try/catch, the eye icon is a single toggle with aria-pressed, and the no-flash class is set by
// the inline script in layouts/app.html.
//
// Privacy mode blurs every amount. Each amount carries the privacy-sensitive class (web/amounts.py),
// and styles/privacy-mode.css blurs them while <html> has the privacy-mode class.
import { Controller } from "/static/vendor/stimulus.js";

const STORAGE_KEY = "omsync-privacy-mode";

function readStorage() {
  try {
    return localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function writeStorage(active) {
  try {
    localStorage.setItem(STORAGE_KEY, String(active));
  } catch {
    // Storage is blocked: the choice lasts for this page only.
  }
}

export default class extends Controller {
  static targets = ["toggle"];

  connect() {
    this.active = readStorage();
    this.apply();
  }

  toggle() {
    this.active = !this.active;
    writeStorage(this.active);
    this.apply();
  }

  apply() {
    document.documentElement.classList.toggle("privacy-mode", this.active);
    this.toggleTargets.forEach((button) => {
      button.setAttribute("aria-pressed", String(this.active));
    });
  }
}
