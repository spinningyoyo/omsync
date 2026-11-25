// The theme choice, shared by the user menu (controllers/theme_controller.js) and the sign-in page
// (controllers/nursery_controller.js): light, dark, or auto by the computer's clock. The rule is the
// nursery's (vendor/nursery/garden/daylight.js); layouts/app.html applies it before first paint.
// The choice is saved in localStorage under "omsync-theme".
import { Daylight } from "/static/vendor/nursery/garden/daylight.js";

export const daylight = new Daylight({ key: "omsync-theme" });
