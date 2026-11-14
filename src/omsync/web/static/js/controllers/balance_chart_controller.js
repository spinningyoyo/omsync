// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/time_series_chart_controller.js.
// Changes: one balance series from a data attribute (no period, brush, Turbo, or utils import); D3 is
// the global from static/vendor/d3.min.js (ISC), loaded only by the account page; the tooltip is
// built with textContent. Colours are design tokens, so light and dark themes both work.
//
// The chart draws statement ledger balances, one point per date. Its tooltip shows the date and the
// amount as the server formatted it. Nothing here writes data.
import { Controller } from "/static/vendor/stimulus.js";

const MARGIN = { top: 12, right: 8, bottom: 26, left: 8 };
const MIN_SIZE = 50;
const TOOLTIP_CLASSES =
  "pointer-events-none absolute z-10 flex flex-col gap-0.5 rounded-lg bg-container px-3 py-2 text-xs shadow-border-md";
const FLAT = "var(--color-gray-400)";

// The stroke colour: green when the balance ends higher than it began, red when lower, grey if flat.
function trendColor(values) {
  const first = values[0];
  const last = values[values.length - 1];
  if (last === first) return FLAT;
  return last > first ? "var(--color-success)" : "var(--color-destructive)";
}

export default class extends Controller {
  static values = { points: Array };

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
    if (!d3 || this.pointsValue.length === 0) return;

    const width = this.element.clientWidth;
    const height = this.element.clientHeight;
    if (width < MIN_SIZE || height < MIN_SIZE) return;

    const parseDay = d3.timeParse("%Y-%m-%d");
    const data = this.pointsValue.map((point) => ({
      date: parseDay(point.date),
      label: point.label,
      formatted: point.formatted,
      cents: point.cents,
    }));
    const values = data.map((datum) => datum.cents);
    const color = trendColor(values);

    const first = data[0].date;
    const last = data[data.length - 1].date;
    const x = d3
      .scaleTime()
      .domain(+first === +last ? [d3.timeDay.offset(first, -1), d3.timeDay.offset(last, 1)] : [first, last])
      .range([MARGIN.left, width - MARGIN.right]);

    let low = d3.min(values);
    let high = d3.max(values);
    if (low === high) {
      const pad = high === 0 ? 100 : Math.abs(high) * 0.5;
      low -= pad;
      high += pad;
    } else {
      const pad = (high - low) * 0.12;
      low -= pad;
      high += pad;
    }
    const y = d3
      .scaleLinear()
      .domain([low, high])
      .range([height - MARGIN.bottom, MARGIN.top]);

    const svg = d3
      .select(this.element)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .attr("viewBox", [0, 0, width, height])
      .attr("class", "block overflow-visible")
      .attr("aria-hidden", "true");

    const area = d3
      .area()
      .x((datum) => x(datum.date))
      .y0(height - MARGIN.bottom)
      .y1((datum) => y(datum.cents));
    svg.append("path").datum(data).attr("d", area).style("fill", color).attr("fill-opacity", 0.08);

    const line = d3
      .line()
      .x((datum) => x(datum.date))
      .y((datum) => y(datum.cents));
    svg
      .append("path")
      .datum(data)
      .attr("d", line)
      .attr("fill", "none")
      .attr("stroke-width", 2)
      .attr("stroke-linejoin", "round")
      .attr("stroke-linecap", "round")
      .style("stroke", color);

    if (data.length === 1) {
      // A line needs two points, so a lone balance is marked with a dot.
      svg.append("circle").attr("r", 4).attr("cx", x(first)).attr("cy", y(data[0].cents)).style("fill", color);
    }

    const labelStyle = (text) =>
      text.style("fill", "var(--utility-text-secondary)").style("font-size", "12px").style("font-weight", "500");
    labelStyle(
      svg
        .append("text")
        .attr("x", MARGIN.left)
        .attr("y", height - 6)
        .attr("text-anchor", "start")
        .text(data[0].label),
    );
    if (data.length > 1) {
      labelStyle(
        svg
          .append("text")
          .attr("x", width - MARGIN.right)
          .attr("y", height - 6)
          .attr("text-anchor", "end")
          .text(data[data.length - 1].label),
      );
    }

    const guide = svg
      .append("line")
      .attr("y1", MARGIN.top)
      .attr("y2", height - MARGIN.bottom)
      .attr("stroke-dasharray", "4 4")
      .style("stroke", "var(--utility-text-subdued)")
      .style("display", "none");
    const dot = svg
      .append("circle")
      .attr("r", 4)
      .style("fill", color)
      .style("stroke", "var(--color-container)")
      .style("stroke-width", 2)
      .style("display", "none")
      .attr("pointer-events", "none");

    const tooltip = d3
      .select(this.element)
      .append("div")
      .attr("class", TOOLTIP_CLASSES)
      .style("opacity", 0)
      .style("top", "0px")
      .style("left", "0px");

    const bisectDate = d3.bisector((datum) => datum.date).center;
    const nearest = (date) => {
      const index = bisectDate(data, date);
      const before = data[Math.max(index - 1, 0)];
      const after = data[Math.min(index, data.length - 1)];
      return date - before.date > after.date - date ? after : before;
    };

    const show = (datum) => {
      const cx = x(datum.date);
      const cy = y(datum.cents);
      guide.attr("x1", cx).attr("x2", cx).style("display", null);
      dot.attr("cx", cx).attr("cy", cy).style("display", null);
      tooltip.selectAll("*").remove();
      tooltip.append("span").attr("class", "text-secondary").text(datum.label);
      tooltip.append("span").attr("class", "font-medium tabular-nums text-primary").text(datum.formatted);
      const tipWidth = tooltip.node().offsetWidth;
      const left = cx + 12 + tipWidth > width ? cx - tipWidth - 12 : cx + 12;
      tooltip
        .style("left", `${Math.max(0, left)}px`)
        .style("top", `${Math.max(0, cy - 44)}px`)
        .style("opacity", 1);
    };
    const hide = () => {
      guide.style("display", "none");
      dot.style("display", "none");
      tooltip.style("opacity", 0);
    };

    svg
      .append("rect")
      .attr("width", width)
      .attr("height", height)
      .attr("fill", "none")
      .attr("pointer-events", "all")
      .on("pointermove", (event) => {
        const [px] = d3.pointer(event);
        show(nearest(x.invert(px)));
      })
      .on("pointerleave", hide);
  }
}
