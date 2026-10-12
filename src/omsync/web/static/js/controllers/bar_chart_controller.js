// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/bar_chart_controller.js.
// Changes: one bar per income and expense, drawn from zero, with a net line through the months
// (Sure draws only the bars); no Turbo, no utils import; D3 is the global from
// static/vendor/d3.min.js (ISC), loaded by the reports page only; the tooltip is built with
// textContent; the amounts arrive already formatted by the server, so the chart never rounds money.
//
// Each data point is { label, income, expense, net, income_text, expense_text, net_text, partial }.
// A partial month (the one still filling) is drawn faded. Nothing here writes data.
import { Controller } from "/static/vendor/stimulus.js";

const MARGIN = { top: 12, right: 8, bottom: 26, left: 8 };
const MIN_SIZE = 50;
const TOOLTIP_CLASSES =
  "pointer-events-none absolute z-10 flex flex-col gap-0.5 rounded-lg bg-container px-3 py-2 text-xs shadow-border-md";
const INCOME = "var(--color-success)";
const EXPENSE = "var(--color-gray-400)";
const NET = "var(--utility-text-primary)";
const SUBDUED = "var(--utility-text-subdued)";

export default class extends Controller {
  static values = { data: Array };

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
    const data = this.dataValue;
    if (!d3 || data.length === 0) return;

    const width = this.element.clientWidth;
    const height = this.element.clientHeight;
    if (width < MIN_SIZE || height < MIN_SIZE) return;

    const innerWidth = width - MARGIN.left - MARGIN.right;
    const innerHeight = height - MARGIN.top - MARGIN.bottom;

    const values = data.flatMap((d) => [d.income, d.expense, d.net]);
    const high = Math.max(0, ...values);
    const low = Math.min(0, ...values);
    const y = d3
      .scaleLinear()
      .domain([low < 0 ? low * 1.1 : 0, high > 0 ? high * 1.1 : 1])
      .range([innerHeight, 0]);
    const zero = y(0);

    const x = d3
      .scaleBand()
      .domain(data.map((d) => d.label))
      .range([0, innerWidth])
      .padding(0.3);
    const within = d3
      .scaleBand()
      .domain(["income", "expense"])
      .range([0, x.bandwidth()])
      .padding(0.15);

    const svg = d3
      .select(this.element)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .attr("viewBox", [0, 0, width, height])
      .attr("class", "block overflow-visible")
      .attr("aria-hidden", "true");
    const group = svg.append("g").attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);

    group
      .append("line")
      .attr("x1", 0)
      .attr("x2", innerWidth)
      .attr("y1", zero)
      .attr("y2", zero)
      .style("stroke", SUBDUED);

    const months = group
      .selectAll("g.month")
      .data(data)
      .join("g")
      .attr("class", "month")
      .attr("transform", (d) => `translate(${x(d.label)},0)`);

    months
      .selectAll("rect.bar")
      .data((d) => [
        { key: "income", value: d.income, month: d },
        { key: "expense", value: d.expense, month: d },
      ])
      .join("rect")
      .attr("class", "bar")
      .attr("x", (d) => within(d.key))
      .attr("y", (d) => Math.min(y(d.value), zero))
      .attr("width", within.bandwidth())
      .attr("height", (d) => Math.abs(y(d.value) - zero))
      .attr("rx", 3)
      .attr("fill", (d) => (d.key === "income" ? INCOME : EXPENSE))
      .attr("fill-opacity", (d) => (d.month.partial ? 0.5 : 1));

    const netX = (d) => x(d.label) + x.bandwidth() / 2;
    group
      .append("path")
      .datum(data)
      .attr("fill", "none")
      .attr("stroke-width", 2)
      .attr("stroke-linejoin", "round")
      .attr("stroke-linecap", "round")
      .style("stroke", NET)
      .attr(
        "d",
        d3
          .line()
          .x(netX)
          .y((d) => y(d.net)),
      );
    group
      .selectAll("circle.net")
      .data(data)
      .join("circle")
      .attr("class", "net")
      .attr("cx", netX)
      .attr("cy", (d) => y(d.net))
      .attr("r", 3)
      .style("fill", NET)
      .style("stroke", "var(--color-container)")
      .style("stroke-width", 1.5);

    group
      .append("g")
      .attr("transform", `translate(0,${innerHeight})`)
      .call(d3.axisBottom(x).tickSize(0))
      .call((g) => g.select(".domain").remove())
      .selectAll("text")
      .style("fill", SUBDUED)
      .style("font-size", "12px")
      .style("font-weight", "500");

    const tooltip = d3
      .select(this.element)
      .append("div")
      .attr("class", TOOLTIP_CLASSES)
      .style("opacity", 0)
      .style("top", "0px")
      .style("left", "0px");

    const show = (event, datum) => {
      tooltip.selectAll("*").remove();
      tooltip.append("span").attr("class", "text-secondary").text(datum.label);
      for (const [name, text] of [
        ["Income", datum.income_text],
        ["Expenses", datum.expense_text],
        ["Net", datum.net_text],
      ]) {
        tooltip
          .append("span")
          .attr("class", "tabular-nums text-primary")
          .text(`${name}: ${text}`);
      }
      const [px] = d3.pointer(event, this.element);
      const tipWidth = tooltip.node().offsetWidth;
      const left = px + 12 + tipWidth > width ? px - tipWidth - 12 : px + 12;
      tooltip.style("left", `${Math.max(0, left)}px`).style("top", "0px").style("opacity", 1);
    };
    const hide = () => tooltip.style("opacity", 0);

    months
      .append("rect")
      .attr("class", "hover")
      .attr("x", 0)
      .attr("y", 0)
      .attr("width", x.bandwidth())
      .attr("height", innerHeight)
      .attr("fill", "transparent")
      .attr("pointer-events", "all")
      .on("pointermove", (event, datum) => show(event, datum))
      .on("pointerleave", hide);
  }
}
