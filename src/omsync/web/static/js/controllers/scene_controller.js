// The scene choice in the user menu (layouts/shell.html). It is the same choice the sign-in page
// makes (controllers/nursery_controller.js): both save it in the nursery's garden memory under
// "omsync-scene", so the sign-in page shows the scene last picked in the app. In the app the scene
// only tints the page background, through data-tint on <html> and styles/tint.css. Cards, tables
// and charts keep their own colours.
//
// The shell holds one menu on wide screens and another in the phone header, so the controller sits
// on the shell and draws the nursery's picker into both, and both agree.

import { GardenMemory } from "/static/vendor/nursery/garden/memory.js";
import { ScenePicker } from "/static/vendor/nursery/garden/picker.js";
import { Controller } from "/static/vendor/stimulus.js";

const memory = new GardenMemory({ sceneKey: "omsync-scene", growthKey: "omsync-scene-growth" });

export default class extends Controller {
  static targets = ["picker"];

  // layouts/shell.html sets data-tint before first paint, so it already holds the current scene.
  connect() {
    const current = document.documentElement.dataset.tint;
    this.pickers = this.pickerTargets.map(
      (root) => new ScenePicker(root, { current, onChoose: (scene) => this.choose(scene) }),
    );
  }

  disconnect() {
    for (const picker of this.pickers) picker.destroy();
  }

  choose(scene) {
    document.documentElement.dataset.tint = scene.id;
    memory.saveScene(scene.id);
    for (const picker of this.pickers) picker.mark(scene.id);
  }
}
