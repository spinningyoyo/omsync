// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/theme_controller.js
// and app/views/layouts/_dark_mode_check.html.erb. Changes: a three-way choice (light, dark, auto
// by the clock, the same as the sign-in page's; see ../theme.js), and no server-side preference.
import { Controller } from "/static/vendor/stimulus.js";
import { applyTheme, chooseTheme, CLOCK_MS, currentChoice } from "../theme.js";

// Three-way switch, the same icon buttons as the sign-in page's. Each option carries
// data-theme-choice and data-theme-target="option". The controller sits on the shell, so the
// switches in the rail's and the phone header's menus agree.
export default class extends Controller {
  static targets = ["option"];

  connect() {
    this.clock = setInterval(applyTheme, CLOCK_MS);
    this.render();
  }

  disconnect() {
    clearInterval(this.clock);
  }

  choose(event) {
    chooseTheme(event.currentTarget.dataset.themeChoice);
    this.render();
  }

  render() {
    const choice = currentChoice();
    this.optionTargets.forEach((option) => {
      option.setAttribute("aria-pressed", String(option.dataset.themeChoice === choice));
    });
  }
}
