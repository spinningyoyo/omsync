// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/dashboard_sortable_controller.js.
// Changes: the grab handle is each section's header, not the whole card; the drop point is the first
// section whose middle is below the pointer (Sure picks the nearest centre, so a drop just below a
// card lands above it); the layout is saved as one POST of order, collapsed, half-width sections, and
// chosen heights; touch hold-to-drag is left out, and the keyboard covers reordering. The keyboard is Sure's: Enter or
// Space grabs, the arrow keys move, and Enter, Space or Escape drops. A live region announces moves.
// Sections can sit two to a row: a drop is placed by reading order, so over a half-width section the
// pointer's side of its middle decides, and over a full-width one its height does.
import { Controller } from "/static/vendor/stimulus.js";

const LAYOUT_URL = "/home/layout";
const SECTION = "[data-dashboard-sortable-target='section']";
const PLACE_ABOVE = "border-t-4";
const PLACE_LEFT = "border-l-4";
const PLACE_BELOW = "border-b-4";
const PLACE_RIGHT = "border-r-4";
const MARK = "border-primary";

export default class extends Controller {
  static targets = ["section", "list", "status"];

  connect() {
    this.dragged = null;
    this.grabbed = null;
  }

  // Mouse and trackpad drag, from a section's header.
  dragStart(event) {
    const section = event.currentTarget.closest(SECTION);
    if (!section) return;
    this.dragged = section;
    section.classList.add("opacity-50");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", section.dataset.sectionKey);
    event.dataTransfer.setDragImage(section, 24, 24);
  }

  dragEnd(event) {
    event.currentTarget.closest(SECTION)?.classList.remove("opacity-50");
    this.dragged = null;
    this.clearMarks();
  }

  dragOver(event) {
    if (!this.dragged) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    this.mark(this.insertionPoint(event));
  }

  drop(event) {
    if (!this.dragged) return;
    event.preventDefault();
    this.clearMarks();
    this.place(this.dragged, this.insertionPoint(event).before);
    this.dragged = null;
    this.save();
  }

  // Keyboard reordering, on a section's header only. Keys pressed on a control inside the header,
  // such as the collapse button, belong to that control.
  handleKeyDown(event) {
    const header = event.currentTarget;
    if (event.target !== header) return;
    const section = header.closest(SECTION);
    switch (event.key) {
      case "Enter":
      case " ":
        event.preventDefault();
        if (this.grabbed === section) {
          this.release();
        } else {
          this.grab(section);
        }
        break;
      case "ArrowUp":
      case "ArrowLeft":
        if (this.grabbed === section) {
          event.preventDefault();
          this.step(section, -1);
        }
        break;
      case "ArrowDown":
      case "ArrowRight":
        if (this.grabbed === section) {
          event.preventDefault();
          this.step(section, 1);
        }
        break;
      case "Escape":
        if (this.grabbed) {
          event.preventDefault();
          this.release();
        }
        break;
    }
  }

  grab(section) {
    if (this.grabbed) this.release();
    this.grabbed = section;
    section.querySelector(":scope > header")?.setAttribute("aria-grabbed", "true");
    section.classList.add("ring-2", "ring-primary", "ring-offset-2");
    this.announce(`${section.querySelector("h2")?.textContent ?? "Section"} grabbed. Use the arrow keys to move it.`);
  }

  release() {
    const section = this.grabbed;
    if (!section) return;
    section.querySelector(":scope > header")?.setAttribute("aria-grabbed", "false");
    section.classList.remove("ring-2", "ring-primary", "ring-offset-2");
    this.grabbed = null;
    this.announce(`Dropped at position ${this.position(section)} of ${this.sectionTargets.length}.`);
    this.save();
  }

  step(section, direction) {
    const sibling = direction < 0 ? section.previousElementSibling : section.nextElementSibling;
    if (!sibling?.matches(SECTION)) return;
    if (direction < 0) {
      this.listTarget.insertBefore(section, sibling);
    } else {
      this.listTarget.insertBefore(sibling, section);
    }
    section.querySelector(":scope > header")?.focus();
    this.announce(`Position ${this.position(section)} of ${this.sectionTargets.length}.`);
  }

  // Where a drop at the pointer lands: the first section, other than the one moving, that the pointer
  // is before in reading order, and whether the pointer is beside it rather than above it. A null
  // section means the end of the list.
  insertionPoint({ clientX, clientY }) {
    const twoUp = this.twoUp();
    const others = this.sectionTargets.filter((section) => section !== this.dragged);
    for (const section of others) {
      const rect = section.getBoundingClientRect();
      if (clientY < rect.top) return { before: section, beside: false };
      if (clientY >= rect.bottom) continue;
      if (twoUp && section.dataset.width === "half") {
        if (clientX < rect.left + rect.width / 2) return { before: section, beside: true };
      } else if (clientY < rect.top + rect.height / 2) {
        return { before: section, beside: false };
      }
    }
    return { before: null, beside: false };
  }

  // True while the page is laid out in two columns, which needs a wide enough container; the grid's
  // computed columns say so, so the rule is not repeated here.
  twoUp() {
    return getComputedStyle(this.listTarget).gridTemplateColumns.split(" ").length > 1;
  }

  place(section, before) {
    if (before) {
      this.listTarget.insertBefore(section, before);
    } else {
      this.listTarget.appendChild(section);
    }
  }

  mark({ before, beside }) {
    this.clearMarks();
    if (before) {
      before.classList.add(beside ? PLACE_LEFT : PLACE_ABOVE, MARK);
    } else {
      const last = this.sectionTargets.filter((section) => section !== this.dragged).at(-1);
      const half = this.twoUp() && last?.dataset.width === "half";
      last?.classList.add(half ? PLACE_RIGHT : PLACE_BELOW, MARK);
    }
  }

  clearMarks() {
    for (const section of this.sectionTargets) {
      section.classList.remove(PLACE_ABOVE, PLACE_LEFT, PLACE_BELOW, PLACE_RIGHT, MARK);
    }
  }

  // The size menu's choices. Width and height are kept in the section's data-width and data-height
  // attributes, which the grid and chart classes read, so the card changes at once.
  setWidth(event) {
    this.setSize(event, "width", event.params.width);
  }

  setHeight(event) {
    this.setSize(event, "height", event.params.height);
  }

  setSize(event, name, value) {
    const section = event.currentTarget.closest(SECTION);
    if (!section || section.dataset[name] === value) return;
    section.dataset[name] = value;
    const group = event.currentTarget.closest("[data-size]");
    for (const segment of group.querySelectorAll("button")) {
      const active = segment === event.currentTarget;
      segment.classList.toggle("segmented-control__segment--active", active);
      segment.setAttribute("aria-pressed", String(active));
    }
    const title = section.querySelector("h2")?.textContent ?? "Section";
    this.announce(`${title}: ${name} ${event.currentTarget.textContent.trim().toLowerCase()}.`);
    this.save();
  }

  position(section) {
    return this.sectionTargets.indexOf(section) + 1;
  }

  announce(message) {
    if (this.hasStatusTarget) this.statusTarget.textContent = message;
  }

  // Saves the whole layout as it stands on the page: the order, which sections are collapsed, which
  // are half width, and the heights other than auto. Each section's collapsed state is kept in its
  // data-collapsed attribute by dashboard_section, its width in data-width, and its height in
  // data-height, present only on a section that offers the choice.
  async save() {
    const order = this.sectionTargets.map((section) => section.dataset.sectionKey);
    const collapsed = this.sectionTargets
      .filter((section) => section.dataset.collapsed === "true")
      .map((section) => section.dataset.sectionKey);
    const half = this.sectionTargets
      .filter((section) => section.dataset.width === "half")
      .map((section) => section.dataset.sectionKey);
    const heights = Object.fromEntries(
      this.sectionTargets
        .filter((section) => section.dataset.height && section.dataset.height !== "auto")
        .map((section) => [section.dataset.sectionKey, section.dataset.height]),
    );
    try {
      const response = await fetch(LAYOUT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ layout: JSON.stringify({ order, collapsed, half, heights }) }),
      });
      if (!response.ok) {
        console.error("[Home layout] The layout was not saved:", response.status);
      }
    } catch (error) {
      console.error("[Home layout] Network error saving the layout:", error);
    }
  }
}
