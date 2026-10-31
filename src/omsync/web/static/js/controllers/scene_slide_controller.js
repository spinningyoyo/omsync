// The Scene item in the user menu (layouts/shell.html). It slides the scene choices out beside the
// menu on the rail, or below it in the phone header. The choices are the buttons that
// controllers/scene_controller.js wires up. Hovering or focusing one shows its name, as on the
// sign-in page.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  static targets = ["toggle", "panel"];

  // The menu hides on any click inside it and on Escape. The choices should hide with it, so the
  // next time the menu opens they start closed.
  connect() {
    this.menu = this.element.closest('[role="menu"]');
    this.observer = new MutationObserver(() => {
      if (this.menu.classList.contains("hidden")) this.close();
    });
    this.observer.observe(this.menu, { attributes: true, attributeFilter: ["class"] });
  }

  disconnect() {
    this.observer.disconnect();
  }

  // The menu closes on a click anywhere inside it, so the toggle stops the click here to stay open.
  toggle(event) {
    event.stopPropagation();
    if (this.panelTarget.classList.contains("hidden")) {
      this.open();
    } else {
      this.close();
    }
  }

  open() {
    this.panelTarget.classList.remove("hidden");
    this.toggleTarget.setAttribute("aria-expanded", "true");
  }

  close() {
    this.panelTarget.classList.add("hidden");
    this.toggleTarget.setAttribute("aria-expanded", "false");
  }
}
