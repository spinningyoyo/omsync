# Icons

The icons are [Lucide](https://lucide.dev) 1.48.0, taken from the `lucide-static` npm package
(published 2026-09-24) at `https://unpkg.com/lucide-static@1.48.0/icons/<name>.svg`.

- License: ISC. The full text is in `LICENSE-lucide.txt`. Copyright (c) 2026 Lucide Icons
  and Contributors.
- The whole set is vendored: every `icons/<name>.svg` of the package, 2,118 files, so a category's
  icon can be any Lucide icon and the picker works offline. `tags.json` is the package's search
  tags, which the picker matches along with names. The package's SHA-512 integrity was checked
  against the npm registry when it was downloaded. Sure's icon names are mapped to Lucide 1.x
  names: `more-horizontal` becomes `ellipsis`, `more-vertical` becomes `ellipsis-vertical`,
  `check-circle` becomes `circle-check`, `x-circle` becomes `circle-x`, and `alert-triangle`
  becomes `triangle-alert`.
- Category icons default to `shapes`. Templates use the `category_icon` macro in
  `components/icon.html`, which falls back to the default for a stored name whose file is missing.

The `icon_svg` helper in `web/icons.py` inlines each file with `currentColor` strokes and
`aria-hidden`. app.py registers it as a Jinja global. Only file names matching `[a-z0-9-]+` are
accepted. To update the set, replace the files from a newer `lucide-static` release and update the
version above. No rebuild is needed, because the helper reads the files at runtime.
