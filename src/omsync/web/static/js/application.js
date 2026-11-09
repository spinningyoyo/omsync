// Starts Stimulus and registers the design-system controllers. layouts/app.html loads this
// file once, after htmx.min.js.
import { Application } from "/static/vendor/stimulus.js";
import accountsPanel from "./controllers/accounts_panel_controller.js";
import autoSubmit from "./controllers/auto_submit_controller.js";
import balanceChart from "./controllers/balance_chart_controller.js";
import barChart from "./controllers/bar_chart_controller.js";
import categories from "./controllers/categories_controller.js";
import dashboardSection from "./controllers/dashboard_section_controller.js";
import dashboardSortable from "./controllers/dashboard_sortable_controller.js";
import dateRange from "./controllers/date_range_controller.js";
import dialog from "./controllers/dialog_controller.js";
import donutChart from "./controllers/donut_chart_controller.js";
import filter from "./controllers/filter_controller.js";
import forest from "./controllers/forest_controller.js";
import guides from "./controllers/guides_controller.js";
import keySuggest from "./controllers/key_suggest_controller.js";
import sankeyChart from "./controllers/sankey_chart_controller.js";
import spendingChart from "./controllers/spending_chart_controller.js";
import menu from "./controllers/menu_controller.js";
import pageDialogs from "./controllers/page_dialogs_controller.js";
import passwordToggle from "./controllers/password_toggle_controller.js";
import popover from "./controllers/popover_controller.js";
import privacyMode from "./controllers/privacy_mode_controller.js";
import reconcile from "./controllers/reconcile_controller.js";
import registerTable from "./controllers/register_controller.js";
import scene from "./controllers/scene_controller.js";
import sceneSlide from "./controllers/scene_slide_controller.js";
import selection from "./controllers/selection_controller.js";
import tabs from "./controllers/tabs_controller.js";
import theme from "./controllers/theme_controller.js";
import toast from "./controllers/toast_controller.js";

// Settle in the same task as the swap, so focus is never restored a frame late.
htmx.config.defaultSettleDelay = 0;
// htmx ignores 4xx bodies by default. Validation errors (422) and conflicts (409, a stale
// change or an undo that no longer applies) come back as fragments to swap in.
htmx.config.responseHandling = [
  { code: "204", swap: false },
  { code: "[23]..", swap: true },
  { code: "400", swap: true, error: false },
  { code: "422", swap: true, error: false },
  { code: "409", swap: true, error: false },
  { code: "[45]..", swap: false, error: true },
];

const application = Application.start();
application.register("accounts-panel", accountsPanel);
application.register("auto-submit", autoSubmit);
application.register("balance-chart", balanceChart);
application.register("bar-chart", barChart);
application.register("categories", categories);
application.register("dashboard-section", dashboardSection);
application.register("dashboard-sortable", dashboardSortable);
application.register("date-range", dateRange);
application.register("dialog", dialog);
application.register("donut-chart", donutChart);
application.register("sankey-chart", sankeyChart);
application.register("spending-chart", spendingChart);
application.register("menu", menu);
application.register("page-dialogs", pageDialogs);
application.register("password-toggle", passwordToggle);
application.register("popover", popover);
application.register("privacy-mode", privacyMode);
application.register("reconcile", reconcile);
application.register("filter", filter);
application.register("forest", forest);
application.register("guides", guides);
application.register("key-suggest", keySuggest);
application.register("register", registerTable);
application.register("scene", scene);
application.register("scene-slide", sceneSlide);
application.register("selection", selection);
application.register("tabs", tabs);
application.register("theme", theme);
application.register("toast", toast);

// A request that fails without a fragment to show (a fault, a lost connection, or the
// omsync:error event that web/errors.py sends) gets a toast, so a save never fails silently.
function showError(message) {
  let region = document.getElementById("error-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "error-region";
    region.setAttribute("aria-live", "assertive");
    document.body.append(region);
  }
  const note = document.createElement("div");
  note.className = "toast toast-error";
  note.setAttribute("role", "alert");
  note.dataset.controller = "toast";
  note.textContent = message;
  region.replaceChildren(note);
}

document.addEventListener("htmx:responseError", (event) => {
  const text = event.detail.xhr.responseText.trim();
  const plain = event.detail.xhr.getResponseHeader("Content-Type")?.startsWith("text/plain");
  showError(plain && text ? text : "Something went wrong, and nothing was saved. Try again.");
});
document.addEventListener("htmx:sendError", () => {
  showError("omsync did not answer. Check that the server is still running, then try again.");
});
document.addEventListener("omsync:error", (event) => showError(event.detail.value));

// A full page load on a slow link leaves the old page in place with no sign of progress, so a
// bar runs along the top until the next page arrives. A download never replaces the page, so the
// bar also stops on its own.
const NAVIGATING_MS = 8000;
let navigatingTimer;
function startNavigating() {
  document.documentElement.classList.add("navigating");
  clearTimeout(navigatingTimer);
  navigatingTimer = setTimeout(stopNavigating, NAVIGATING_MS);
}
function stopNavigating() {
  document.documentElement.classList.remove("navigating");
}
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href]");
  if (!link || event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (link.target || link.hasAttribute("download") || link.hasAttribute("hx-get")) return;
  const url = new URL(link.href, location.href);
  if (url.origin !== location.origin || (url.pathname === location.pathname && url.hash)) return;
  startNavigating();
});
document.addEventListener("submit", (event) => {
  const form = event.target;
  // A form with data-confirm asks first, for an action that cannot be undone.
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }
  if (event.defaultPrevented || form.method === "dialog") return;
  if (form.matches("[hx-post],[hx-get],[hx-put],[hx-delete],[data-register-form]")) return;
  startNavigating();
  if (form.method !== "post") return;
  // A second press would send the form again while the first is still on its way.
  form.querySelectorAll("button[type=submit]").forEach((button) => {
    button.setAttribute("aria-disabled", "true");
    button.addEventListener("click", (again) => again.preventDefault());
  });
});
// Back and forward restore a page from the cache with the bar still showing.
window.addEventListener("pageshow", stopNavigating);
