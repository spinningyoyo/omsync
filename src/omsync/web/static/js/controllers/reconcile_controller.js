// The Reconcile page's manual link: choosing the counterpart sets the amount to the smaller of the
// two unlinked remainders, which is the most a link can explain. Amounts are integer cents here,
// and the server checks the amount it receives again.
import { Controller } from "/static/vendor/stimulus.js";

export default class extends Controller {
  pickCandidate(event) {
    const radio = event.target;
    if (!radio.matches?.("input[type=radio][data-remaining]")) return;
    const form = radio.closest("form");
    const source = Number(form?.dataset.sourceRemaining);
    const other = Number(radio.dataset.remaining);
    const amount = document.getElementById("reconcile-link-amount");
    if (!amount || !Number.isInteger(source) || !Number.isInteger(other)) return;
    amount.value = dollars(Math.min(source, other));
  }
}

function dollars(cents) {
  const abs = Math.abs(cents);
  return `${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}
