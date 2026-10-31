// The sign-in page's easter egg (layouts/auth.html). Clicking the elephant waters a pixel scene:
// a fountain sprays from its trunk and, after a short cooldown, the scene grows one stage, up to
// MAX_STAGE. The scene is drawn by forest_scene.js. The picker in the corner swaps the scene, and
// the second group sets day, night or auto, which follows the clock: the app's theme choice, shared
// with the user menu (../theme.js). The last button resets the scene on show to stage 0.
//
// Two things are remembered in the browser: the scene last picked and how far each scene has grown.
// A first visit shows a random scene.
import { Controller } from "/static/vendor/stimulus.js";
import { COOLDOWN_MS, Forest, MAX_STAGE, SCENES, WATER_MS } from "../forest_scene.js";
import { clamp, iconSvg } from "../forest/pixel.js";
import { applyTheme, chooseTheme, CLOCK_MS, currentChoice } from "../theme.js";

const MASCOT_CELLS = 32; // the mascot's grid width; its trunk tip is at cell (2, 3)
const SCENE_KEY = "omsync-scene";
const GROWTH_KEY = "omsync-scene-growth";

function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage can be blocked; the choice then lasts until the page reloads.
  }
}

function savedGrowth() {
  try {
    const growth = JSON.parse(recall(GROWTH_KEY));
    return growth && typeof growth === "object" ? growth : {};
  } catch {
    return {};
  }
}

export default class extends Controller {
  static targets = ["elephant", "ground", "water", "bloom", "status", "scene", "mode"];

  // layouts/auth.html sets data-scene, and layouts/app.html data-theme, before first paint, so the
  // page never flashes the wrong colours.
  connect() {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const scene = SCENES.find((s) => s.id === this.element.dataset.scene) ?? SCENES[0];
    const stage = this.savedStage(scene.id);
    this.forest = new Forest({ ground: this.groundTarget, water: this.waterTarget, reducedMotion, scene, stage });
    this.readyAt = 0;
    this.bloomTarget.style.opacity = String(stage / MAX_STAGE);

    for (const button of this.sceneTargets) {
      const own = SCENES.find((s) => s.id === button.dataset.forestIdParam);
      if (own) button.innerHTML = iconSvg(own.icon.grid, own.icon.colours);
    }
    this.markScene(scene.id);
    this.markMode();
    this.clock = setInterval(applyTheme, CLOCK_MS);
  }

  disconnect() {
    clearTimeout(this.splashTimer);
    clearInterval(this.clock);
    this.forest.destroy();
  }

  water() {
    const now = performance.now();
    if (now < this.readyAt) return;
    this.readyAt = now + COOLDOWN_MS;

    const mascot = this.elephantTarget.querySelector("svg").getBoundingClientRect();
    const cell = mascot.width / MASCOT_CELLS;
    const before = this.forest.stage;
    const stage = this.forest.sprinkle(mascot.left + cell * 2, mascot.top + cell * 3);

    this.bloomTarget.style.opacity = String(stage / MAX_STAGE);
    if (stage !== before) {
      this.saveStage(stage);
      this.statusTarget.textContent =
        stage === MAX_STAGE ? "The scene is fully grown." : `Watered ${stage} of ${MAX_STAGE}.`;
    }

    this.elephantTarget.classList.add("is-watering");
    clearTimeout(this.splashTimer);
    this.splashTimer = setTimeout(() => this.elephantTarget.classList.remove("is-watering"), WATER_MS);
  }

  // The picker. A scene comes back as grown as it was when it was last left.
  scene(event) {
    const scene = SCENES.find((s) => s.id === event.params.id);
    if (!scene || scene.id === this.element.dataset.scene) return;
    const stage = this.savedStage(scene.id);
    this.element.dataset.scene = scene.id;
    this.forest.setScene(scene, stage);
    this.bloomTarget.style.opacity = String(stage / MAX_STAGE);
    this.markScene(scene.id);
    this.statusTarget.textContent = `${event.currentTarget.getAttribute("aria-label")} scene.`;
    remember(SCENE_KEY, scene.id);
  }

  // Starts the scene on show over, as a new layout at stage 0, and forgets how far it had grown.
  reset() {
    const id = this.element.dataset.scene;
    this.forest.setScene(SCENES.find((s) => s.id === id), 0);
    this.bloomTarget.style.opacity = "0";
    this.saveStage(0);
    this.readyAt = 0;
    const own = this.sceneTargets.find((button) => button.dataset.forestIdParam === id);
    this.statusTarget.textContent = `${own.getAttribute("aria-label")} reset.`;
  }

  // Day, night, or auto by the computer's clock.
  setMode(event) {
    chooseTheme(event.params.mode);
    this.markMode();
    this.statusTarget.textContent = `${event.currentTarget.getAttribute("aria-label")}.`;
  }

  savedStage(id) {
    return clamp(Math.round(Number(savedGrowth()[id]) || 0), 0, MAX_STAGE);
  }

  saveStage(stage) {
    remember(GROWTH_KEY, JSON.stringify({ ...savedGrowth(), [this.element.dataset.scene]: stage }));
  }

  markScene(id) {
    for (const button of this.sceneTargets) {
      button.setAttribute("aria-pressed", String(button.dataset.forestIdParam === id));
    }
  }

  markMode() {
    for (const button of this.modeTargets) {
      button.setAttribute("aria-pressed", String(button.dataset.forestModeParam === currentChoice()));
    }
  }
}
