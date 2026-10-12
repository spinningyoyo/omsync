# Vendored JavaScript

Pinned copies of third-party files served from `/static/vendor/`. To upgrade, download the
new file from the URL below, replace it, and update this table, including the checksum.

| File | Name | Exact version | Source URL | License | sha256 |
|---|---|---|---|---|---|
| `htmx.min.js` | htmx | 2.0.4 | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | 0BSD ([license](https://unpkg.com/htmx.org@2.0.4/LICENSE)) | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `stimulus.js` | Stimulus | 3.2.2 | https://unpkg.com/@hotwired/stimulus@3.2.2/dist/stimulus.js | MIT, Copyright 2021 Basecamp, LLC ([license](https://unpkg.com/@hotwired/stimulus@3.2.2/LICENSE.md)) | `23deecdac6f36c08e8f39fbed6b27f60c850cacad3a34eb71194462b7b1647f3` |
| `d3.min.js` | D3 (UMD build, sets the global `d3`; loaded by the account, reports, and home pages) | 7.9.0 | https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js | ISC, Copyright 2010-2023 Mike Bostock ([license](https://cdn.jsdelivr.net/npm/d3@7.9.0/LICENSE)) | `f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539` |
| `d3-sankey.min.js` | d3-sankey (UMD build; adds `sankey` to the global `d3`, so it loads after `d3.min.js`; loaded by the home page only) | 0.12.3 | https://cdn.jsdelivr.net/npm/d3-sankey@0.12.3/dist/d3-sankey.min.js | BSD-3-Clause ([license](https://cdn.jsdelivr.net/npm/d3-sankey@0.12.3/LICENSE)) | `8286db5d6aa049cc6e8a546708943b79dfb4daaefb0ccf42af674ec0ee4c86be` |

Notes:

- The banner comment at the top of `stimulus.js` still reads "Stimulus 3.2.1". The npm
  package `@hotwired/stimulus` at version 3.2.2 ships that file unchanged, and its
  `package.json` says 3.2.2, so 3.2.2 is the pinned version.
