# Icons

The icons are [Lucide](https://lucide.dev) 1.48.0, taken from the `lucide-static` npm package
(published 2026-09-24) at `https://unpkg.com/lucide-static@1.48.0/icons/<name>.svg`.

- License: ISC. The full text is in `LICENSE-lucide.txt`. Copyright (c) 2026 Lucide Icons
  and Contributors.
- Only the icons the components and the gallery use are vendored. Sure's icon names are
  mapped to Lucide 1.x names: `more-horizontal` becomes `ellipsis`, `more-vertical` becomes
  `ellipsis-vertical`, `check-circle` becomes `circle-check`, `x-circle` becomes `circle-x`, and
  `alert-triangle` becomes `triangle-alert`.
- Files: chart-pie, check, chevron-down, chevron-right, circle-check, circle-x,
  banknote-arrow-down, corner-down-right, credit-card, ellipsis, ellipsis-vertical, house, info, land-plot, log-out, monitor,
  moon, panel-left, pencil, plus, repeat, scale, search, settings, sun, tags, target, trash-2,
  triangle-alert, x, eye, eye-off. The shell's rail uses house, chart-pie, credit-card, repeat,
  scale, tags and land-plot. The privacy toggle uses eye and eye-off. The reports page adds printer, trending-up, and trending-down. The home page adds
  grip-vertical, the section drag handle. The Plan pages add archive, car, chevron-left, copy,
  gift, graduation-cap, heart, piggy-bank, plane, rotate-ccw, shield, and wallet. The goal form's
  icon choices are piggy-bank, shield, plane, car, house, graduation-cap, gift, heart, wallet, and
  target. The Bank sync shortcut uses banknote-arrow-down.

The `icon_svg` helper in `web/icons.py` inlines each file with `currentColor` strokes and
`aria-hidden`. app.py registers it as a Jinja global. Only file names matching `[a-z0-9-]+` are
accepted. To add an icon, download its SVG from the URL above and add its name to this list. No
rebuild is needed, because the helper reads the files at runtime.
