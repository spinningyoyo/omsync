// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/donut_chart_controller.js.
// Changes: a fixed-size ring with no resize, Turbo, or drill-down clicks (the legend holds the links);
// hovering a segment or a legend row shows that category and dims the rest, and leaving restores the
// total. Segment order is the data's order, and the legend row says which segment it draws. Names and
// amounts arrive formatted from the server. D3 is the global from static/vendor/d3.min.js (ISC).
import { Controller } from "/static/vendor/stimulus.js";

const DIMMED = 0.35;
const RING = { inner: 34, outer: 48, pad: 0.012, corner: 2 };

export default class extends Controller {
  static targets = ["chart", "label", "amount"];
  static values = { data: Object };

  connect() {
    // Privacy mode blurs the whole chart, since its amounts are drawn, not text (styles/privacy-mode.css).
    this.element.classList.add("privacy-sensitive");
    this.totalLabel = this.labelTarget.textContent;
    this.totalAmount = this.amountTarget.textContent;
    // Stimulus can connect before the deferred D3 script has run, so the first draw may find no d3.
    // The observer fires once the element is laid out, as the other charts rely on, and draws then.
    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(this.element);
    this.draw();
  }

  disconnect() {
    this.resizeObserver?.disconnect();
    this.chartTarget.replaceChildren();
  }

  draw() {
    const d3 = window.d3;
    this.chartTarget.replaceChildren();
    const segments = this.dataValue.segments ?? [];
    if (!d3 || segments.length === 0) return;

    const pie = d3
      .pie()
      .sortValues(null)
      .value((segment) => segment.cents);
    const arc = d3
      .arc()
      .innerRadius(RING.inner)
      .outerRadius(RING.outer)
      .padAngle(RING.pad)
      .cornerRadius(RING.corner);

    const svg = d3
      .select(this.chartTarget)
      .append("svg")
      .attr("viewBox", "-50 -50 100 100")
      .attr("class", "block h-full w-full")
      .attr("aria-hidden", "true");
    this.paths = svg
      .append("g")
      .selectAll("path")
      .data(pie(segments))
      .join("path")
      .attr("d", arc)
      .style("fill", (arcDatum) => arcDatum.data.color)
      .style("cursor", "default")
      .style("transition", "opacity 150ms ease")
      .on("mouseenter", (event, arcDatum) => this.show(arcDatum.index))
      .on("mouseleave", () => this.reset());
  }

  // A legend row. Rows that draw no segment carry -1 and do nothing.
  highlight(event) {
    const index = Number(event.currentTarget.dataset.segment);
    if (Number.isInteger(index) && index >= 0) this.show(index);
  }

  reset() {
    this.labelTarget.textContent = this.totalLabel;
    this.amountTarget.textContent = this.totalAmount;
    this.paths?.style("opacity", 1);
  }

  show(index) {
    const segment = this.dataValue.segments?.[index];
    if (!segment) return;
    this.labelTarget.textContent = segment.name;
    this.amountTarget.textContent = segment.text;
    this.paths?.style("opacity", (arcDatum) => (arcDatum.index === index ? 1 : DIMMED));
  }
}
