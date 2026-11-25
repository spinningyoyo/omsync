// The sign-in page's easter egg (layouts/auth.html). The elephant waters a pixel scene. The
// nursery's garden does the work (static/vendor/nursery/garden/): the scene, its growth and the
// elephant's click and hold (garden.js), and the scene popover in the corner (picker.js). This
// controller says what each change means on this page: the status line, the tip and the stage
// sprout. The second group sets day, night or auto, the app's theme choice shared with the user
// menu (../theme.js). The last button resets the scene on show.
//
// The tip follows the stage: stage 0 shows the first tip, and any watering shows the tip that
// teaches the hold. A first visit shows a random scene.

import { Garden } from "/static/vendor/nursery/garden/garden.js";
import { GardenMemory } from "/static/vendor/nursery/garden/memory.js";
import { ScenePopover } from "/static/vendor/nursery/garden/picker.js";
import { MAX_STAGE } from "/static/vendor/nursery/nursery.js";
import { Controller } from "/static/vendor/stimulus.js";
import { daylight } from "../theme.js";

const MASCOT_CELLS = 32; // the mascot's grid width; its trunk tip is at cell (2, 3)

const trunkTip = (box) => {
  const cell = box.width / MASCOT_CELLS;
  return [box.left + cell * 2, box.top + cell * 3];
};

export default class extends Controller {
  static targets = ["elephant", "ground", "water", "bloom", "status", "picker", "mode", "tip", "stage", "stageIcon"];

  // layouts/auth.html sets data-scene, and layouts/app.html data-theme, before first paint, so the
  // page never flashes the wrong colours.
  connect() {
    this.garden = new Garden({
      ground: this.groundTarget,
      water: this.waterTarget,
      bloom: this.bloomTarget,
      sceneId: this.element.dataset.scene,
      memory: new GardenMemory({ sceneKey: "omsync-scene", growthKey: "omsync-scene-growth" }),
      reducedMotion: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      mascot: { button: this.elephantTarget, spout: trunkTip },
      onFill: (step) => this.say(`Filled ${step} of ${MAX_STAGE}.`),
      onChange: (change) => this.changed(change),
    });
    this.popover = new ScenePopover(this.pickerTarget, {
      current: this.garden.scene.id,
      onChoose: (scene) => this.garden.choose(scene),
    });
    this.markMode();
    daylight.start();
  }

  disconnect() {
    daylight.stop();
    this.popover.destroy();
    this.garden.destroy();
  }

  changed({ scene, stage, before, cause, stageShown }) {
    this.element.dataset.scene = scene.id;
    this.popover?.mark(scene.id);
    this.markStage(stage, stageShown);
    if (cause === "choose") this.say(`${scene.label} scene.`);
    else if (cause === "reset") this.say(`${scene.label} reset.`);
    else if (cause === "water" && stage !== before) {
      this.say(stage === MAX_STAGE ? "The scene is fully grown." : `Watered ${stage} of ${MAX_STAGE}.`);
    }
  }

  reset() {
    this.garden.reset();
  }

  // Day, night, or auto by the computer's clock.
  setMode(event) {
    daylight.choose(event.params.mode);
    this.markMode();
    this.say(`${event.currentTarget.getAttribute("aria-label")}.`);
  }

  say(text) {
    this.statusTarget.textContent = text;
  }

  // The sprout under Reset shows the stage, one of nine icons. The tip is the stage's: stage 0
  // shows the first tip, and any watering shows the hold tip.
  markStage(stage, shown) {
    this.stageTarget.hidden = !shown;
    const text = `Stage ${stage} of ${MAX_STAGE}`;
    this.stageTarget.setAttribute("aria-label", text);
    this.stageTarget.dataset.tip = text;
    for (const icon of this.stageIconTargets) icon.hidden = Number(icon.dataset.stage) !== stage;
    this.tipTarget.textContent = stage >= 1 ? this.tipTarget.dataset.after : this.tipTarget.dataset.before;
  }

  markMode() {
    for (const button of this.modeTargets) {
      button.setAttribute("aria-pressed", String(button.dataset.nurseryModeParam === daylight.choice()));
    }
  }
}
