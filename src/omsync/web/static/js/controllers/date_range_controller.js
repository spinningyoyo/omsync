// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/auto_submit_form_controller.js,
// as the reports page's custom range uses it. Changes: the form is sent only once both dates are
// whole and in order. A date field reports a change for every digit typed into its year, so year
// 0002, 0020 and 0202 would each reload the page on the way to 2026; Sure sends each of them.
import { Controller } from "/static/vendor/stimulus.js";

const WHOLE_DATE = /^\d{4}-\d{2}-\d{2}$/;
const FIRST_YEAR = 1900;

export default class extends Controller {
  static targets = ["start", "end"];

  connect() {
    this.sent = { start: this.startTarget.value, end: this.endTarget.value };
  }

  // A change: send when the range is whole, in order, and not the one already shown.
  apply() {
    const { start, end } = this.range();
    if (!this.ready(start, end)) return;
    if (start === this.sent.start && end === this.sent.end) return;
    this.send(start, end);
  }

  // Enter sends whatever is there, so a range the page cannot use gets its error message.
  applyNow(event) {
    event.preventDefault();
    const { start, end } = this.range();
    this.send(start, end);
  }

  range() {
    return { start: this.startTarget.value, end: this.endTarget.value };
  }

  ready(start, end) {
    const whole = (value) => WHOLE_DATE.test(value) && Number(value.slice(0, 4)) >= FIRST_YEAR;
    return whole(start) && whole(end) && start <= end;
  }

  send(start, end) {
    this.sent = { start, end };
    this.element.requestSubmit();
  }
}
