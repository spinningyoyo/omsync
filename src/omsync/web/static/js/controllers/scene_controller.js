// The scene choice in the user menu (layouts/shell.html). It is the same choice the sign-in page
// makes (controllers/nursery_controller.js): both save it in localStorage under "omsync-scene", so
// the sign-in page shows the scene last picked in the app. In the app the scene only tints the
// page background, through data-tint on <html> and styles/tint.css. Cards, tables and charts keep
// their own colours.
//
// The shell holds one menu on wide screens and another in the phone header, so the controller sits
// on the shell and both sets of scene buttons agree. Storage can be blocked, so every access is in
// try/catch.
import { Controller } from "/static/vendor/stimulus.js";
import { iconSvg } from "/static/vendor/nursery/kit/pixel.js";
import { SCENES } from "/static/vendor/nursery/nursery.js";

const SCENE_KEY = "omsync-scene";

function remember(id) {
  try {
    localStorage.setItem(SCENE_KEY, id);
  } catch {
    // Storage is blocked: the choice lasts for this page.
  }
}

export default class extends Controller {
  static targets = ["scene"];

  // layouts/shell.html sets data-tint before first paint, so it already holds the current scene.
  connect() {
    for (const button of this.sceneTargets) {
      const own = SCENES.find((s) => s.id === button.dataset.sceneIdParam);
      if (own) button.innerHTML = iconSvg(own.icon.grid, own.icon.colours);
    }
    this.markScene(document.documentElement.dataset.tint);
  }

  choose(event) {
    const scene = SCENES.find((s) => s.id === event.params.id);
    if (!scene) return;
    document.documentElement.dataset.tint = scene.id;
    remember(scene.id);
    this.markScene(scene.id);
  }

  markScene(id) {
    for (const button of this.sceneTargets) {
      button.setAttribute("aria-pressed", String(button.dataset.sceneIdParam === id));
    }
  }
}
