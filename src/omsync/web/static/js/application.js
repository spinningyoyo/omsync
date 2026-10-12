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
import dialog from "./controllers/dialog_controller.js";
import donutChart from "./controllers/donut_chart_controller.js";
import filter from "./controllers/filter_controller.js";
import keySuggest from "./controllers/key_suggest_controller.js";
import sankeyChart from "./controllers/sankey_chart_controller.js";
import spendingChart from "./controllers/spending_chart_controller.js";
import menu from "./controllers/menu_controller.js";
import pageDialogs from "./controllers/page_dialogs_controller.js";
import passwordToggle from "./controllers/password_toggle_controller.js";
import privacyMode from "./controllers/privacy_mode_controller.js";
import reconcile from "./controllers/reconcile_controller.js";
import registerTable from "./controllers/register_controller.js";
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
application.register("dialog", dialog);
application.register("donut-chart", donutChart);
application.register("sankey-chart", sankeyChart);
application.register("spending-chart", spendingChart);
application.register("menu", menu);
application.register("page-dialogs", pageDialogs);
application.register("password-toggle", passwordToggle);
application.register("privacy-mode", privacyMode);
application.register("reconcile", reconcile);
application.register("filter", filter);
application.register("key-suggest", keySuggest);
application.register("register", registerTable);
application.register("selection", selection);
application.register("tabs", tabs);
application.register("theme", theme);
application.register("toast", toast);
