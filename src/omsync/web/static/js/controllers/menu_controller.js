// Adapted from Sure (we-promise/sure), AGPL-3.0: app/components/DS/menu_controller.js
// Changes: the panel is positioned with CSS instead of floating-ui, and there are no Turbo hooks.
import { Controller } from "/static/vendor/stimulus.js";

const ITEM_SELECTOR = '[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"]';

// Strict action-list menu: role="menu" holds role="menuitem" items. Arrow keys, Home and End
// move focus between items. Escape closes the menu and returns focus to the trigger.
export default class extends Controller {
  static targets = ["button", "content"];

  connect() {
    this.close();
  }

  get isOpen() {
    return !this.contentTarget.classList.contains("hidden");
  }

  get items() {
    return Array.from(this.contentTarget.querySelectorAll(ITEM_SELECTOR));
  }

  toggle() {
    if (this.isOpen) {
      this.close();
    } else {
      this.open();
    }
  }

  open() {
    this.contentTarget.classList.remove("hidden");
    this.buttonTarget.setAttribute("aria-expanded", "true");
    this.focusItem(this.items[0]);
  }

  close() {
    this.contentTarget.classList.add("hidden");
    this.buttonTarget.setAttribute("aria-expanded", "false");
  }

  clickOutside(event) {
    if (this.isOpen && !this.element.contains(event.target)) this.close();
  }

  keydown(event) {
    if (event.key === "Escape") {
      if (this.isOpen) {
        this.close();
        this.buttonTarget.focus();
      }
      return;
    }
    if (!this.isOpen) return;

    const items = this.items;
    const index = items.indexOf(event.target);
    let next;
    switch (event.key) {
      case "ArrowDown":
        next = index < 0 ? 0 : (index + 1) % items.length;
        break;
      case "ArrowUp":
        next = index < 0 ? items.length - 1 : (index - 1 + items.length) % items.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = items.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    this.focusItem(items[next]);
  }

  focusItem(item) {
    if (!item) return;
    this.items.forEach((el) => {
      el.setAttribute("tabindex", el === item ? "0" : "-1");
    });
    item.focus();
  }
}
