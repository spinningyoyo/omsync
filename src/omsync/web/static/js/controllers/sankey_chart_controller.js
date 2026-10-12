// Adapted from Sure (we-promise/sure), AGPL-3.0: app/javascript/controllers/sankey_chart_controller.js.
// Changes: one static diagram, with no zoom or Turbo; ribbons leaving the green hub shade from green
// to their category's colour, blended in OKLab so the middle stays clean, and ribbons arriving keep
// their source's colour; source nodes are labelled on their left and sink nodes on their
// right, so labels never sit on a ribbon, and the labels on each side are spread so none overlaps,
// each joined to its box by a leader line in the box's colour; the centre node is labelled above;
// the chart is as tall as its labels need; hovering a node or ribbon dims what it does not touch,
// labels included; a category node opens the register for it; names, amounts, shares, and links
// arrive from the server, and tooltips are built with textContent. d3-sankey is the global added by
// static/vendor/d3-sankey.min.js (BSD-3-Clause), loaded by the home page only. Nothing here writes data.
import { Controller } from "/static/vendor/stimulus.js";

const MARGIN_TOP = 44;
const MARGIN_BOTTOM = 8;
const NODE_WIDTH = 12;
const NODE_RADIUS = 3;
const NODE_PADDING = 14;
const MIN_WIDTH = 240;
const MIN_HEIGHT = 160;
const CHART_MIN_HEIGHT = 360;
const CHART_MAX_HEIGHT = 720;
const LABEL_GAP = 28; // the least distance between two labels on one side
const LABEL_LINE = 12; // from a label's name line to its amount line
const LABEL_OFFSET = 16; // from a box to its label, which the leader line spans
const RIBBON_OPACITY = 0.55;
const BLEND_STEPS = [0.2, 0.4, 0.6, 0.8]; // between a ribbon's two colours, so the blend is OKLab
const DIMMED_OPACITY = 0.2;
const FADE_MS = 400;
const TOOLTIP_CLASSES =
  "pointer-events-none absolute z-10 flex flex-col gap-0.5 rounded-lg bg-container px-3 py-2 text-xs shadow-border-md";

export default class extends Controller {
  static values = { data: Object };

  connect() {
    // Privacy mode blurs the whole chart, since its amounts are drawn, not text (styles/privacy-mode.css).
    this.element.classList.add("privacy-sensitive");
    this.gradientPrefix = `sankey-${Math.random().toString(36).slice(2)}`;
    this.drawn = false;
    this.element.style.height = `${chartHeight(this.dataValue)}px`;
    // The observer reports the element's size as soon as it is watched, which draws the first time.
    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(this.element);
  }

  disconnect() {
    this.resizeObserver?.disconnect();
    this.element.replaceChildren();
  }

  draw() {
    const d3 = window.d3;
    this.element.replaceChildren();
    if (!d3?.sankey) return;
    const { nodes = [], links = [] } = this.dataValue;
    if (nodes.length === 0 || links.length === 0) return;

    const width = this.element.clientWidth;
    const height = this.element.clientHeight;
    if (width < MIN_WIDTH || height < MIN_HEIGHT) return;

    // Room on each side for the labels of the first and last columns.
    const labelWidth = Math.min(160, Math.max(96, Math.round(width * 0.22)));
    const graph = d3
      .sankey()
      .nodeWidth(NODE_WIDTH)
      .nodePadding(NODE_PADDING)
      .extent([
        [labelWidth, MARGIN_TOP],
        [width - labelWidth, height - MARGIN_BOTTOM],
      ])({
      nodes: nodes.map((node) => ({ ...node })),
      links: links.map((link) => ({ ...link })),
    });

    const svg = d3
      .select(this.element)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .attr("viewBox", [0, 0, width, height])
      .attr("class", "block overflow-visible")
      .attr("aria-hidden", "true");

    // A ribbon leaving the hub is painted with a gradient from the hub's colour to its target's. One
    // arriving at the hub keeps its source's colour. Gradients use page coordinates, so a ribbon's
    // own slope does not tilt them, and the blend is stepped through color-mix in OKLab, because the
    // browser's own interpolation is in sRGB, which turns green toward violet or orange muddy.
    const defs = svg.append("defs");
    graph.links.forEach((link, index) => {
      const from = link.source.color;
      const to = isHub(link.source) ? link.target.color : from;
      const gradient = defs
        .append("linearGradient")
        .attr("id", `${this.gradientPrefix}-${index}`)
        .attr("gradientUnits", "userSpaceOnUse")
        .attr("x1", link.source.x1)
        .attr("x2", link.target.x0);
      const stops = [[0, from], ...BLEND_STEPS.map((t) => [t, blend(from, to, t)]), [1, to]];
      for (const [offset, colour] of stops) {
        gradient
          .append("stop")
          .attr("offset", offset)
          .style("stop-color", colour)
          .style("stop-opacity", RIBBON_OPACITY);
      }
    });

    const linkPaths = svg
      .append("g")
      .attr("fill", "none")
      .selectAll("path")
      .data(graph.links)
      .join("path")
      .attr("class", "sankey-link")
      .attr("d", (link) =>
        d3.linkHorizontal()({
          source: [link.source.x1, link.y0],
          target: [link.target.x0, link.y1],
        }),
      )
      .attr("stroke-width", (link) => Math.max(1, link.width))
      .attr("stroke", (_link, index) => `url(#${this.gradientPrefix}-${index})`);

    const nodeGroups = svg
      .append("g")
      .selectAll("g")
      .data(graph.nodes)
      .join("g")
      .attr("class", "sankey-node");

    // A node with a link is wrapped in a real anchor, so it can be opened in a new tab. The anchor
    // is out of the tab order: the chart is hidden from assistive technology, and the Outflows list
    // carries the same links for the keyboard.
    nodeGroups.each(function (node) {
      const holder = asLink(d3.select(this), node);
      holder
        .append("rect")
        .attr("x", node.x0)
        .attr("y", node.y0)
        .attr("width", node.x1 - node.x0)
        .attr("height", Math.max(1, node.y1 - node.y0))
        .attr("rx", NODE_RADIUS)
        .style("fill", node.color);
    });

    const { labels, leaders } = drawLabels(svg, graph.nodes, labelWidth, height);

    const tooltip = d3
      .select(this.element)
      .append("div")
      .attr("class", TOOLTIP_CLASSES)
      .style("opacity", 0)
      .style("top", "0px")
      .style("left", "0px");

    // The first line names what is pointed at, then its figures, then a hint when it can be opened.
    const show = (event, lines, hint = "") => {
      tooltip.selectAll("*").remove();
      for (const [index, line] of lines.entries()) {
        tooltip
          .append("span")
          .attr("class", index === 0 ? "text-secondary" : "tabular-nums text-primary")
          .text(line);
      }
      if (hint) tooltip.append("span").attr("class", "text-secondary").text(hint);
      const [px, py] = d3.pointer(event, this.element);
      const tipWidth = tooltip.node().offsetWidth;
      const left = px + 12 + tipWidth > width ? px - tipWidth - 12 : px + 12;
      tooltip
        .style("left", `${Math.max(0, left)}px`)
        .style("top", `${Math.max(0, py - 16)}px`)
        .style("opacity", 1);
    };
    const hide = () => tooltip.style("opacity", 0);

    // Keeps the links and nodes that pass their predicate at full strength and dims the rest, labels
    // and leader lines included, so what is pointed at stands out.
    const dimOthers = (keepLink, keepNode) => {
      linkPaths.attr("stroke-opacity", (link) => (keepLink(link) ? 1 : DIMMED_OPACITY));
      for (const part of [nodeGroups, labels, leaders]) {
        part.style("opacity", (node) => (keepNode(node) ? 1 : DIMMED_OPACITY));
      }
    };
    const reset = () => {
      linkPaths.attr("stroke-opacity", 1);
      for (const part of [nodeGroups, labels, leaders]) part.style("opacity", 1);
      hide();
    };

    linkPaths
      .on("mouseenter", (event, link) => {
        dimOthers(
          (other) => other === link,
          (node) => node === link.source || node === link.target,
        );
        const lines = [`${link.source.name} to ${link.target.name}`, link.text];
        const share = link.target.share ?? link.source.share;
        if (share) lines.push(`${share} of spending`);
        show(event, lines);
      })
      .on("mousemove", (event) => {
        const [px, py] = d3.pointer(event, this.element);
        tooltip.style("left", `${px + 12}px`).style("top", `${Math.max(0, py - 16)}px`);
      })
      .on("mouseleave", reset);

    const pointAtNode = (event, node) => {
      const linked = new Set([
        node,
        ...node.sourceLinks.map((link) => link.target),
        ...node.targetLinks.map((link) => link.source),
      ]);
      dimOthers(
        (link) => link.source === node || link.target === node,
        (other) => linked.has(other),
      );
      const lines = [node.name, node.text];
      if (node.detail) lines.push(node.detail);
      if (node.share) lines.push(`${node.share} of spending`);
      show(event, lines, linkTarget(node) ? "Open these transactions" : "");
    };
    nodeGroups.selectAll("rect").on("mouseenter", pointAtNode).on("mouseleave", reset);
    labels.on("mouseenter", pointAtNode).on("mouseleave", reset);

    // The first drawing fades in; later ones, from resizing, do not replay it.
    if (!this.drawn && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      svg.style("opacity", 0);
      requestAnimationFrame(() =>
        svg.style("transition", `opacity ${FADE_MS}ms ease-out`).style("opacity", 1),
      );
    }
    this.drawn = true;
  }
}

// The height the chart needs: a label slot for each box on its busier side, within limits.
function chartHeight({ nodes = [], links = [] }) {
  const targets = new Set(links.map((link) => link.target));
  const sources = new Set(links.map((link) => link.source));
  const left = nodes.filter((_node, index) => !targets.has(index)).length;
  const right = nodes.filter((_node, index) => !sources.has(index)).length;
  const needed = Math.max(left, right) * LABEL_GAP + MARGIN_TOP + MARGIN_BOTTOM + LABEL_LINE;
  return Math.min(CHART_MAX_HEIGHT, Math.max(CHART_MIN_HEIGHT, needed));
}

// The colour `t` of the way from one colour to another, mixed in OKLab. Each is a CSS colour.
function blend(from, to, t) {
  return from === to ? from : `color-mix(in oklab, ${from} ${Math.round((1 - t) * 100)}%, ${to})`;
}

// The box every ribbon passes through: it both receives and gives.
function isHub(node) {
  return node.sourceLinks.length > 0 && node.targetLinks.length > 0;
}

// A path on this site, or null. The server builds these, but a link is only followed if it stays here.
function linkTarget(node) {
  const href = node.href;
  return typeof href === "string" && href.startsWith("/") && !href.startsWith("//") ? href : null;
}

// The selection to draw into: the node's own group, or an anchor inside it when the node has a link.
function asLink(selection, node) {
  const href = linkTarget(node);
  if (!href) return selection;
  return selection
    .append("a")
    .attr("href", href)
    .attr("tabindex", -1)
    .style("cursor", "pointer");
}

// Source nodes are labelled on their left, sinks on their right, and the centre node above itself.
// The labels on each side are spread, so none overlaps the one above it, and a leader line in the
// box's colour joins each to its box, however far the spreading has moved it. Returns the label and
// leader selections, each bound to its node.
function drawLabels(svg, nodes, labelWidth, height) {
  const sources = nodes.filter((node) => node.targetLinks.length === 0);
  const sinks = nodes.filter((node) => node.sourceLinks.length === 0);
  const firstLine = new Map([...spread(sources, height), ...spread(sinks, height)]);
  const maxChars = Math.max(10, Math.floor((labelWidth - LABEL_OFFSET) / 7));

  const leaderGroup = svg.append("g").attr("fill", "none");
  const group = svg.append("g").attr("class", "select-none");
  for (const node of nodes) {
    const sink = node.sourceLinks.length === 0;
    const source = node.targetLinks.length === 0;
    const middle = !source && !sink;
    const edge = sink ? node.x1 : node.x0;
    const direction = sink ? 1 : -1;
    const x = middle ? (node.x0 + node.x1) / 2 : edge + direction * LABEL_OFFSET;
    const y = middle ? node.y0 - LABEL_LINE * 2 : firstLine.get(node);
    const anchor = middle ? "middle" : sink ? "start" : "end";
    const shown = node.name.length > maxChars ? `${node.name.slice(0, maxChars - 1)}…` : node.name;

    if (!middle) {
      const from = [edge + direction, (node.y0 + node.y1) / 2];
      const to = [x - direction * 2, y + LABEL_LINE / 2 - 2];
      const bend = (from[0] + to[0]) / 2;
      leaderGroup
        .append("path")
        .datum(node)
        .attr("d", `M${from[0]},${from[1]}C${bend},${from[1]} ${bend},${to[1]} ${to[0]},${to[1]}`)
        .attr("stroke", node.color)
        .attr("stroke-width", 1)
        .attr("stroke-opacity", 0.8);
    }

    const text = asLink(group, node)
      .append("text")
      .datum(node)
      .attr("x", x)
      .attr("y", y)
      .attr("text-anchor", anchor);
    text.append("title").text(`${node.name}: ${node.text}`);
    text.append("tspan").attr("class", "text-xs font-medium fill-current text-primary").text(shown);
    text
      .append("tspan")
      .attr("x", x)
      .attr("dy", `${LABEL_LINE}px`)
      .attr("class", "tabular-nums fill-current text-secondary")
      .style("font-size", "0.65rem")
      .text(node.text);
  }
  return { labels: group.selectAll("text"), leaders: leaderGroup.selectAll("path") };
}

// The first-line y of each label on one side, in the order the nodes sit, with LABEL_GAP between
// neighbours. A label that would run past the bottom is pulled back up.
function spread(nodes, height) {
  const sorted = [...nodes].sort((a, b) => a.y0 + a.y1 - (b.y0 + b.y1));
  const bottom = height - MARGIN_BOTTOM - LABEL_LINE;
  const ys = sorted.map((node) => (node.y0 + node.y1) / 2);
  for (let i = 1; i < ys.length; i += 1) {
    ys[i] = Math.max(ys[i], ys[i - 1] + LABEL_GAP);
  }
  if (ys.length > 0 && ys[ys.length - 1] > bottom) {
    ys[ys.length - 1] = bottom;
    for (let i = ys.length - 2; i >= 0; i -= 1) {
      ys[i] = Math.min(ys[i], ys[i + 1] - LABEL_GAP);
    }
  }
  return sorted.map((node, i) => [node, ys[i]]);
}
