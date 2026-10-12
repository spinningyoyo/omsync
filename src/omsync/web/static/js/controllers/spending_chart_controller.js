// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/spending_chart_controller.js.
// Changes: two periods of the same length, one point per day, drawn on a shared day axis, with the
// previous period dashed; the tooltip shows the labels and amounts the server formatted, so the chart
// never rounds money; the axis is labelled with the first and last day of this period; no Turbo.
// D3 is the global from static/vendor/d3.min.js (ISC), loaded by the home page. Nothing here writes data.
import { Controller } from "/static/vendor/stimulus.js";

const MARGIN = { top: 12, right: 8, bottom: 24, left: 8 };
const MIN_SIZE = 80;
const CURRENT_COLOR = "var(--color-success)";
const PREVIOUS_COLOR = "var(--color-gray-400)";
const GUIDE_COLOR = "var(--utility-text-subdued)";
const SUBDUED = "var(--utility-text-subdued)";
const TOOLTIP_CLASSES =
  "pointer-events-none absolute z-10 flex flex-col gap-0.5 rounded-lg bg-container px-3 py-2 text-xs shadow-border-md";

export default class extends Controller {
  static values = { data: Object };

  connect() {
    // Privacy mode blurs the whole chart, since its amounts are drawn, not text (styles/privacy-mode.css).
    this.element.classList.add("privacy-sensitive");
    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(this.element);
    this.draw();
  }

  disconnect() {
    this.resizeObserver?.disconnect();
    this.element.replaceChildren();
  }

  draw() {
    const d3 = window.d3;
    this.element.replaceChildren();
    const { current = [], previous = [] } = this.dataValue;
    if (!d3 || (current.length === 0 && previous.length === 0)) return;

    const width = this.element.clientWidth;
    const height = this.element.clientHeight;
    if (width < MIN_SIZE || height < MIN_SIZE) return;

    const days = Math.max(current.length, previous.length);
    const innerWidth = width - MARGIN.left - MARGIN.right;
    const innerHeight = height - MARGIN.top - MARGIN.bottom;
    const values = [0, ...current.map((p) => p.cents), ...previous.map((p) => p.cents)];
    let low = Math.min(...values);
    let high = Math.max(...values);
    if (low === high) high = low + 100;
    const pad = (high - low) * 0.08;

    const x = d3
      .scaleLinear()
      .domain([1, Math.max(days, 2)])
      .range([MARGIN.left, MARGIN.left + innerWidth]);
    const y = d3
      .scaleLinear()
      .domain([low - pad, high + pad])
      .range([MARGIN.top + innerHeight, MARGIN.top]);

    const svg = d3
      .select(this.element)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .attr("viewBox", [0, 0, width, height])
      .attr("class", "block overflow-visible")
      .attr("aria-hidden", "true");

    svg
      .append("line")
      .attr("x1", MARGIN.left)
      .attr("x2", MARGIN.left + innerWidth)
      .attr("y1", y(0))
      .attr("y2", y(0))
      .style("stroke", SUBDUED)
      .attr("stroke-opacity", 0.4);

    const line = d3
      .line()
      .x((point) => x(point.day))
      .y((point) => y(point.cents));

    if (previous.length > 0) {
      svg
        .append("path")
        .datum(previous)
        .attr("fill", "none")
        .attr("stroke-width", 1.5)
        .attr("stroke-dasharray", "4 3")
        .attr("stroke-linejoin", "round")
        .style("stroke", PREVIOUS_COLOR)
        .attr("d", line);
    }
    if (current.length > 0) {
      svg
        .append("path")
        .datum(current)
        .attr("fill", "none")
        .attr("stroke-width", 2)
        .attr("stroke-linejoin", "round")
        .attr("stroke-linecap", "round")
        .style("stroke", CURRENT_COLOR)
        .attr("d", line);
    }

    const axis = (text, px, anchor) =>
      svg
        .append("text")
        .attr("x", px)
        .attr("y", height - 6)
        .attr("text-anchor", anchor)
        .style("fill", SUBDUED)
        .style("font-size", "12px")
        .style("font-weight", "500")
        .text(text);
    if (current.length > 0) {
      axis(current[0].label, MARGIN.left, "start");
      axis(current[current.length - 1].label, MARGIN.left + innerWidth, "end");
    }

    const guide = svg
      .append("line")
      .attr("y1", MARGIN.top)
      .attr("y2", MARGIN.top + innerHeight)
      .attr("stroke-dasharray", "4 4")
      .style("stroke", GUIDE_COLOR)
      .style("display", "none");
    const dotCurrent = svg
      .append("circle")
      .attr("r", 4)
      .style("fill", CURRENT_COLOR)
      .style("stroke", "var(--color-container)")
      .style("stroke-width", 2)
      .style("display", "none");
    const dotPrevious = svg
      .append("circle")
      .attr("r", 3.5)
      .style("fill", PREVIOUS_COLOR)
      .style("stroke", "var(--color-container)")
      .style("stroke-width", 2)
      .style("display", "none");

    const tooltip = d3
      .select(this.element)
      .append("div")
      .attr("class", TOOLTIP_CLASSES)
      .style("opacity", 0)
      .style("top", "0px")
      .style("left", "0px");

    const show = (index) => {
      const now = current[index];
      const before = previous[index];
      const px = x(index + 1);
      guide.attr("x1", px).attr("x2", px).style("display", null);
      if (now) {
        dotCurrent.attr("cx", px).attr("cy", y(now.cents)).style("display", null);
      } else {
        dotCurrent.style("display", "none");
      }
      if (before) {
        dotPrevious.attr("cx", px).attr("cy", y(before.cents)).style("display", null);
      } else {
        dotPrevious.style("display", "none");
      }
      tooltip.selectAll("*").remove();
      tooltip
        .append("span")
        .attr("class", "text-secondary")
        .text(now ? now.label : before?.label ?? "");
      if (now) {
        tooltip
          .append("span")
          .attr("class", "tabular-nums text-primary")
          .text(`This period: ${now.text}`);
      }
      if (before) {
        tooltip
          .append("span")
          .attr("class", "tabular-nums text-secondary")
          .text(`Previous: ${before.text}`);
      }
      const tipWidth = tooltip.node().offsetWidth;
      const anchor = now ?? before;
      const left = px + 12 + tipWidth > width ? px - tipWidth - 12 : px + 12;
      tooltip
        .style("left", `${Math.max(0, left)}px`)
        .style("top", `${Math.max(0, y(anchor.cents) - 44)}px`)
        .style("opacity", 1);
    };
    const hide = () => {
      guide.style("display", "none");
      dotCurrent.style("display", "none");
      dotPrevious.style("display", "none");
      tooltip.style("opacity", 0);
    };

    svg
      .append("rect")
      .attr("x", MARGIN.left)
      .attr("y", 0)
      .attr("width", innerWidth)
      .attr("height", height)
      .attr("fill", "none")
      .attr("pointer-events", "all")
      .on("pointermove", (event) => {
        const [px] = d3.pointer(event, svg.node());
        const day = Math.round(x.invert(px));
        const index = Math.min(Math.max(day - 1, 0), days - 1);
        show(index);
      })
      .on("pointerleave", hide);
  }
}
