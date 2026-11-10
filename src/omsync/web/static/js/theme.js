// The theme choice, shared by the user menu (controllers/theme_controller.js) and the sign-in page
// (controllers/nursery_controller.js): light, dark, or auto, which follows the computer's clock and
// is dark from NIGHT_FROM to NIGHT_UNTIL. It is saved in localStorage under "omsync-theme".
// layouts/app.html applies the same rule before first paint and puts the choice on
// <html data-theme-choice>.
const STORAGE_KEY = "omsync-theme";
const CHOICES = ["light", "dark", "auto"];
const NIGHT_FROM = 17;
const NIGHT_UNTIL = 7;

// How often auto mode looks at the clock.
export const CLOCK_MS = 60_000;

export function currentChoice() {
  const choice = document.documentElement.dataset.themeChoice;
  return CHOICES.includes(choice) ? choice : "auto";
}

export function chooseTheme(choice) {
  document.documentElement.dataset.themeChoice = choice;
  try {
    localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // Storage is blocked (private window, site data cleared): the choice lasts for this page.
  }
  applyTheme();
}

export function applyTheme() {
  const choice = currentChoice();
  const hour = new Date().getHours();
  const night = choice === "dark" || (choice === "auto" && (hour >= NIGHT_FROM || hour < NIGHT_UNTIL));
  const theme = night ? "dark" : "light";
  if (document.documentElement.getAttribute("data-theme") !== theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }
}
