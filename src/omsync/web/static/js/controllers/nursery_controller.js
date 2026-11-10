// The sign-in page's easter egg (layouts/auth.html). Clicking the elephant waters a pixel scene:
// a fountain sprays from its trunk and, after a short cooldown, the scene grows one stage, up to
// MAX_STAGE. Holding the elephant fills it with water instead, one eighth every half second
// (../elephant_water.js); letting go, or the brim at four seconds, waters the scene that many
// stages at once, while the
// water drains over the fountain. The scene is drawn by the nursery's engine
// (static/vendor/nursery/nursery.js). The picker in the corner swaps the scene, and the second
// group sets day, night or auto, which follows the clock: the app's theme choice, shared with the
// user menu (../theme.js). The last button resets the scene on show to stage 0. A sprout under it
// shows the stage once the scene has been watered or reset, or when it was already grown.
//
// Two things are remembered in the browser: the scene last picked, and how far each scene has
// grown. The tip follows the stage: stage 0 shows the first tip, and any watering shows the tip
// that teaches the hold. A first visit shows a random scene.
import { Controller } from "/static/vendor/stimulus.js";
import { COOLDOWN_MS, MAX_STAGE, Nursery, SCENES, WATER_MS, releaseMs, showMs } from "/static/vendor/nursery/nursery.js";
import { clamp, iconSvg } from "/static/vendor/nursery/kit/pixel.js";
import { ElephantWater } from "../elephant_water.js";
import { applyTheme, chooseTheme, CLOCK_MS, currentChoice } from "../theme.js";

const MASCOT_CELLS = 32; // the mascot's grid width; its trunk tip is at cell (2, 3)
const SCENE_KEY = "omsync-scene";
const GROWTH_KEY = "omsync-scene-growth";
const FILL_STEP_MS = 500; // each step of a hold fills one eighth; a full elephant takes MAX_STAGE steps
const HOLD_MS = FILL_STEP_MS; // a shorter press is a plain watering
const BRIM_MS = 750; // the pause at a full elephant before it lets go on its own
const SHAKE_MS = 320;

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
  static targets = ["elephant", "ground", "water", "bloom", "status", "scene", "mode", "tip", "stage", "stageIcon"];

  // layouts/auth.html sets data-scene, and layouts/app.html data-theme, before first paint, so the
  // page never flashes the wrong colours.
  connect() {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const scene = SCENES.find((s) => s.id === this.element.dataset.scene) ?? SCENES[0];
    const stage = this.savedStage(scene.id);
    this.nursery = new Nursery({ ground: this.groundTarget, water: this.waterTarget, reducedMotion, scene, stage });
    this.readyAt = 0;
    this.mode = "idle"; // idle, holding, brim or draining
    this.touched = false; // the scene has been watered or reset in this visit
    this.longPress = false;
    this.reducedMotion = reducedMotion;
    this.fill = new ElephantWater(this.elephantTarget.querySelector("svg"), { reducedMotion });
    this.bloomTarget.style.opacity = String(stage / MAX_STAGE);
    this.markStage(stage);

    for (const button of this.sceneTargets) {
      const own = SCENES.find((s) => s.id === button.dataset.nurseryIdParam);
      if (own) button.innerHTML = iconSvg(own.icon.grid, own.icon.colours);
    }
    this.markScene(scene.id);
    this.markMode();
    this.clock = setInterval(applyTheme, CLOCK_MS);
  }

  disconnect() {
    clearTimeout(this.splashTimer);
    clearTimeout(this.shakeTimer);
    cancelAnimationFrame(this.frames);
    clearInterval(this.clock);
    this.nursery.destroy();
  }

  // A plain watering: one stage and a short fountain.
  water() {
    const now = performance.now();
    if (now < this.readyAt) return;
    this.readyAt = now + COOLDOWN_MS;
    this.sprinkle();

    this.elephantTarget.classList.add("is-watering");
    clearTimeout(this.splashTimer);
    this.splashTimer = setTimeout(() => this.elephantTarget.classList.remove("is-watering"), WATER_MS);
  }

  // A click, from a pointer or from assistive technology. A hold that ended in a release already
  // watered, so the click it leaves behind does nothing. A key press never clicks.
  click() {
    if (this.longPress) this.longPress = false;
    else this.water();
  }

  press(event) {
    if (event.button !== 0 || !event.isPrimary) return;
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Without capture, the release is still seen when it ends on the elephant.
    }
    // A press that begins while a show is on starts nothing, and its click must not water either.
    this.longPress = !this.startHold("pointer");
  }

  // Space and Enter hold like a press. Their default would click, so it is prevented.
  keyDown(event) {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    if (event.repeat) return;
    this.startHold("key");
  }

  keyUp(event) {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    if (this.hold?.source === "key") this.endHold();
  }

  letGo() {
    if (this.hold?.source === "pointer") this.endHold();
  }

  // Focus lost while a key is down: its release would never arrive.
  lose() {
    this.endHold();
  }

  // A long press on a phone would open the context menu.
  hush(event) {
    event.preventDefault();
  }

  startHold(source) {
    const now = performance.now();
    if (this.mode !== "idle" || now < this.readyAt) return false;
    this.mode = "holding";
    this.hold = { source, start: now, step: 0 };
    this.frameTime = now;
    this.fill.clear();
    this.frames = requestAnimationFrame(this.frame);
    return true;
  }

  // Under half a second is a plain watering: a key waters now, and a pointer waters by the click that follows.
  endHold() {
    // A release during the brim does not stop the hold, which lets go on its own. A pointer's click
    // that follows must still be swallowed, as after a finished hold. A key never clicks.
    if (this.mode === "brim") {
      if (this.hold?.source === "pointer" && !this.hold.released) {
        this.hold.released = true;
        this.longPress = true;
      }
      return;
    }
    if (this.mode !== "holding") return;
    const held = performance.now() - this.hold.start;
    if (held >= HOLD_MS) {
      const steps = held / FILL_STEP_MS; // the level in eighths, and the drain's starting height
      this.release(Math.min(MAX_STAGE, Math.floor(steps)), steps);
      return;
    }
    const source = this.hold.source;
    this.stopFrames();
    this.fill.clear();
    if (source === "key") this.water();
  }

  frame = (now) => {
    const dt = Math.max(0, Math.min(0.05, (now - this.frameTime) / 1000));
    this.frameTime = now;
    if (this.mode === "holding") this.fillUp(now, dt);
    else if (this.mode === "brim") {
      if (now >= this.brimUntil) this.release(MAX_STAGE, MAX_STAGE);
      else this.fill.step(dt, MAX_STAGE, true);
    } else if (this.mode === "draining") this.drainDown(now, dt);
    this.fill.render();
    if (this.mode !== "idle") this.frames = requestAnimationFrame(this.frame);
  };

  // The hold is measured in steps of FILL_STEP_MS, one eighth each, so a full elephant is MAX_STAGE steps.
  fillUp(now, dt) {
    const steps = Math.max(0, now - this.hold.start) / FILL_STEP_MS;
    const step = Math.min(MAX_STAGE, Math.floor(steps));
    if (step > this.hold.step) {
      this.hold.step = step;
      this.fill.kick(step);
      this.shake(step);
      this.statusTarget.textContent = `Filled ${step} of ${MAX_STAGE}.`;
    }
    if (steps >= MAX_STAGE) {
      this.mode = "brim";
      this.brimUntil = now + BRIM_MS;
      this.elephantTarget.classList.add("is-brim", "is-squinting");
      this.fill.step(dt, MAX_STAGE, true);
    } else this.fill.step(dt, steps, true);
  }

  // Lets the water go: the scene grows level stages while the elephant drains over the fountain.
  release(level, held) {
    const now = performance.now();
    const classes = this.elephantTarget.classList;
    classes.remove("is-shaking", "is-shaking-hard", "is-brim");
    classes.add("is-squinting");
    clearTimeout(this.shakeTimer);
    this.mode = "draining";
    // A pointer released during the brim has already had its click swallowed, so it is not re-armed.
    this.longPress = this.hold.source === "pointer" && !this.hold.released;
    this.hold = null;
    this.drain = { start: now, ms: releaseMs(level), from: Math.min(held, MAX_STAGE) };
    this.readyAt = now + showMs(level);
    this.sprinkle(level);
  }

  drainDown(now, dt) {
    const done = (now - this.drain.start) / this.drain.ms;
    if (done < 1) {
      this.fill.step(dt, this.drain.from * (1 - done), false);
      return;
    }
    this.fill.clear();
    this.elephantTarget.classList.remove("is-squinting");
    this.mode = "idle";
  }

  stopFrames() {
    cancelAnimationFrame(this.frames);
    this.mode = "idle";
    this.hold = null;
  }

  // A short shake on each step, harder from the fifth.
  shake(step) {
    if (this.reducedMotion) return;
    const classes = this.elephantTarget.classList;
    classes.remove("is-shaking", "is-shaking-hard");
    void this.elephantTarget.offsetWidth; // restarts the animation
    classes.add(step >= 5 ? "is-shaking-hard" : "is-shaking");
    clearTimeout(this.shakeTimer);
    this.shakeTimer = setTimeout(() => classes.remove("is-shaking", "is-shaking-hard"), SHAKE_MS);
  }

  // Waters the scene from the trunk tip: one stage, or level stages for a release. The elephant
  // is at rest by now, so the tip is where the sprite is drawn.
  sprinkle(level) {
    const mascot = this.elephantTarget.querySelector("svg").getBoundingClientRect();
    const cell = mascot.width / MASCOT_CELLS;
    const before = this.nursery.stage;
    const stage = this.nursery.sprinkle(mascot.left + cell * 2, mascot.top + cell * 3, level);

    this.bloomTarget.style.opacity = String(stage / MAX_STAGE);
    if (stage !== before) {
      this.saveStage(stage);
      this.statusTarget.textContent =
        stage === MAX_STAGE ? "The scene is fully grown." : `Watered ${stage} of ${MAX_STAGE}.`;
    }
    this.touched = true;
    this.markStage(stage);
  }

  // The picker. A scene comes back as grown as it was when it was last left.
  scene(event) {
    const scene = SCENES.find((s) => s.id === event.params.id);
    if (!scene || scene.id === this.element.dataset.scene) return;
    const stage = this.savedStage(scene.id);
    this.element.dataset.scene = scene.id;
    this.nursery.setScene(scene, stage);
    this.bloomTarget.style.opacity = String(stage / MAX_STAGE);
    this.markStage(stage);
    this.markScene(scene.id);
    this.statusTarget.textContent = `${event.currentTarget.getAttribute("aria-label")} scene.`;
    remember(SCENE_KEY, scene.id);
  }

  // Starts the scene on show over, as a new layout at stage 0, and forgets how far it had grown.
  reset() {
    const id = this.element.dataset.scene;
    this.nursery.setScene(SCENES.find((s) => s.id === id), 0);
    this.bloomTarget.style.opacity = "0";
    this.saveStage(0);
    this.readyAt = 0;
    this.touched = true;
    this.markStage(0);
    const own = this.sceneTargets.find((button) => button.dataset.nurseryIdParam === id);
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

  // The sprout under Reset shows the stage. It stays hidden until the scene has been watered or
  // reset in this visit, or was already grown.
  markStage(stage) {
    this.stageTarget.hidden = !(this.touched || stage >= 1);
    const text = `Stage ${stage} of ${MAX_STAGE}`;
    this.stageTarget.setAttribute("aria-label", text);
    this.stageTarget.dataset.tip = text;
    for (const icon of this.stageIconTargets) icon.hidden = Number(icon.dataset.stage) !== stage;
    // The tip is the stage's: stage 0 shows the first tip, and any watering shows the hold tip.
    this.tipTarget.textContent = stage >= 1 ? this.tipTarget.dataset.after : this.tipTarget.dataset.before;
  }

  markScene(id) {
    for (const button of this.sceneTargets) {
      button.setAttribute("aria-pressed", String(button.dataset.nurseryIdParam === id));
    }
  }

  markMode() {
    for (const button of this.modeTargets) {
      button.setAttribute("aria-pressed", String(button.dataset.nurseryModeParam === currentChoice()));
    }
  }
}
