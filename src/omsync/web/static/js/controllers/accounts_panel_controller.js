// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/sidebar_resize_controller.js
// and the collapse half of app_layout_controller.js. Changes: one controller for both behaviours, the
// width and collapsed state in localStorage instead of per-user server preferences, keyboard
// resizing, and no Turbo or fetch calls.
// Collapsible, resizable accounts panel for the app shell (layouts/shell.html).
//
// The width (240 to 320 px) and the collapsed state live in localStorage under
// "omsync-accounts-width" and "omsync-accounts-collapsed". The inline script in shell.html applies
// them before first paint, so the panel never flashes at the wrong size. Storage can be blocked
// (private windows, cleared site data), so every access is in try/catch and the panel still works
// for the page's lifetime.
//
// Width is a CSS variable on <html>, which shell.css reads. The collapsed state is an attribute
// on <html> that hides the panel.
import { Controller } from "/static/vendor/stimulus.js";

const WIDTH_KEY = "omsync-accounts-width";
const COLLAPSED_KEY = "omsync-accounts-collapsed";
const MIN_WIDTH = 240;
const MAX_WIDTH = 320;
const DEFAULT_WIDTH = 320;
// Keyboard nudge in pixels for the focused divider.
const KEY_STEP = 16;

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage is blocked: the choice lasts for this page only.
  }
}

function clampWidth(width) {
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(width)));
}

// Below lg the panel is not docked. It is an overlay that starts closed, and its state is not saved.
const NARROW_QUERY = "(max-width: 1023px)";

export default class extends Controller {
  static targets = ["panel", "toggle", "handle"];

  connect() {
    this.dragging = false;
    this.narrow = window.matchMedia(NARROW_QUERY);
    this.onBreakpoint = () => this.syncToggle();
    this.narrow.addEventListener("change", this.onBreakpoint);
    this.syncToggle();
    this.syncHandle();
  }

  disconnect() {
    this.narrow.removeEventListener("change", this.onBreakpoint);
  }

  get root() {
    return document.documentElement;
  }

  get overlayMode() {
    return this.narrow.matches;
  }

  get overlayOpen() {
    return this.root.dataset.accountsOverlay === "true";
  }

  get collapsed() {
    if (this.overlayMode) return !this.overlayOpen;
    return this.root.dataset.accountsCollapsed === "true";
  }

  get width() {
    const value = Number.parseInt(this.root.style.getPropertyValue("--accounts-width"), 10);
    return Number.isFinite(value) ? value : DEFAULT_WIDTH;
  }

  toggle() {
    if (this.overlayMode) {
      this.setOverlay(!this.overlayOpen);
      return;
    }
    const collapsed = !this.collapsed;
    this.root.dataset.accountsCollapsed = String(collapsed);
    writeStorage(COLLAPSED_KEY, String(collapsed));
    this.syncToggle();
  }

  // Opens and closes the overlay below lg. Escape and the backdrop close it too.
  setOverlay(open) {
    this.root.dataset.accountsOverlay = String(open);
    this.syncToggle();
    if (open) {
      this.panelTarget.querySelector("a")?.focus({ preventScroll: true });
    } else if (this.hasToggleTarget && this.overlayMode) {
      this.toggleTarget.focus({ preventScroll: true });
    }
  }

  closeOverlay() {
    if (this.overlayMode && this.overlayOpen) this.setOverlay(false);
  }

  startResize(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    this.dragging = true;
    this.handleTarget.setPointerCapture?.(event.pointerId);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  resize(event) {
    if (!this.dragging) return;
    const left = this.panelTarget.getBoundingClientRect().left;
    this.setWidth(clampWidth(event.clientX - left));
  }

  endResize() {
    if (!this.dragging) return;
    this.dragging = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    writeStorage(WIDTH_KEY, String(this.width));
  }

  resizeKey(event) {
    let next;
    if (event.key === "ArrowRight") {
      next = this.width + KEY_STEP;
    } else if (event.key === "ArrowLeft") {
      next = this.width - KEY_STEP;
    } else if (event.key === "Home") {
      next = DEFAULT_WIDTH;
    } else {
      return;
    }
    event.preventDefault();
    this.setWidth(clampWidth(next));
    writeStorage(WIDTH_KEY, String(this.width));
  }

  resetWidth() {
    this.setWidth(DEFAULT_WIDTH);
    writeStorage(WIDTH_KEY, String(DEFAULT_WIDTH));
  }

  setWidth(width) {
    this.root.style.setProperty("--accounts-width", `${width}px`);
    this.syncHandle();
  }

  syncToggle() {
    if (this.hasToggleTarget) {
      this.toggleTarget.setAttribute("aria-expanded", String(!this.collapsed));
    }
  }

  syncHandle() {
    if (this.hasHandleTarget) {
      this.handleTarget.setAttribute("aria-valuenow", String(this.width));
    }
  }
}
