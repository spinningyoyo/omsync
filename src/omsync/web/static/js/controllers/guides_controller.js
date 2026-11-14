// The Guides: which guides this browser has read, the progress line on the index, and the section
// that the "On this page" list marks as current. The read list is kept in localStorage under
// "omsync-guides-read", as a JSON list of guide slugs. Storage can be blocked or hold junk, so every
// access is guarded, and the pages work without it: a read mark then lasts for one visit.
import { Controller } from "/static/vendor/stimulus.js";

const STORAGE_KEY = "omsync-guides-read";

function readSlugs() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter((slug) => typeof slug === "string") : [];
  } catch {
    return [];
  }
}

function writeSlugs(slugs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(slugs));
  } catch {
    // Storage is blocked: the mark lasts for this page only.
  }
}

export default class extends Controller {
  static targets = ["check", "progress", "mark", "markLabel", "tocLink"];
  static values = { slug: String };

  connect() {
    this.read = new Set(readSlugs());
    this.render();
    this.watchSections();
  }

  disconnect() {
    this.observer?.disconnect();
  }

  toggleRead() {
    if (this.read.has(this.slugValue)) {
      this.read.delete(this.slugValue);
    } else {
      this.read.add(this.slugValue);
    }
    writeSlugs([...this.read]);
    this.render();
  }

  render() {
    this.checkTargets.forEach((badge) => {
      badge.hidden = !this.read.has(badge.dataset.slug);
    });

    if (this.hasProgressTarget) {
      const known = this.checkTargets.map((badge) => badge.dataset.slug);
      const done = known.filter((slug) => this.read.has(slug)).length;
      this.progressTarget.textContent = `${done} of ${known.length} read`;
      this.progressTarget.hidden = false;
    }

    if (this.hasMarkTarget) {
      const done = this.read.has(this.slugValue);
      this.markTarget.setAttribute("aria-pressed", String(done));
      if (this.hasMarkLabelTarget) {
        this.markLabelTarget.textContent = done ? "Marked as read" : "Mark as read";
      }
    }
  }

  // Marks the link of the section nearest the top of the page. A section counts once its heading is
  // in the upper part of the window, so the marked link matches the text being read.
  watchSections() {
    if (!this.hasTocLinkTarget || !("IntersectionObserver" in window)) return;
    const links = new Map(this.tocLinkTargets.map((link) => [link.dataset.id, link]));
    const sections = [...this.element.querySelectorAll("section.guide-section")];
    if (sections.length === 0) return;

    this.observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          links.forEach((link, id) => {
            link.setAttribute("aria-current", String(id === entry.target.id));
          });
        });
      },
      { rootMargin: "0px 0px -65% 0px" },
    );
    sections.forEach((section) => {
      this.observer.observe(section);
    });
  }
}
