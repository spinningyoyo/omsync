# Fonts

Both families are served from `/static/fonts/` and declared in `styles/fonts.css`. Both are
SIL Open Font License 1.1, and each license text is in this folder.

| File | Family and version | Source | License | sha256 |
|---|---|---|---|---|
| `Geist-Variable.woff2` | Geist 1.401, variable weight 100-900 | Copied from Sure (we-promise/sure, commit d55eac2), `app/assets/fonts/geist/Geist[wght].woff2`, renamed to drop the brackets | OFL 1.1, `Geist-OFL.txt` (Copyright 2024 The Geist Project Authors, https://github.com/vercel/geist-font) | `3b380bd2170a39c9031691eb322b452b3e60e1307463dda0f434110360222c7e` |
| `NotoSansThai-thai.woff2` | Noto Sans Thai 2.002, variable weight 100-900, Thai subset | https://fonts.gstatic.com/s/notosansthai/v29/iJWQBXeUZi_OHPqn4wq6hQ2_hbJ1xyN9wd43SofNWcdfKI2hTWsb-P2c.woff2 (Google Fonts build v29) | OFL 1.1, `NotoSansThai-OFL.txt` (Copyright 2022 The Noto Project Authors, https://github.com/notofonts/thai) | `0558c7649eb1625372943ac6ec3f7bf672da383754dc22dc868d68e2b006526c` |
| `NotoSansThai-latin-ext.woff2` | Noto Sans Thai 2.002, variable weight 100-900, Latin Extended subset | https://fonts.gstatic.com/s/notosansthai/v29/iJWQBXeUZi_OHPqn4wq6hQ2_hbJ1xyN9wd43SofNWcdfMo2hTWsb-P2c.woff2 (Google Fonts build v29) | OFL 1.1, `NotoSansThai-OFL.txt` | `fbba4ed12731d3f5d07b9235795df1b6d21e9bca38c7809028686bbbcdb339c0` |

Notes:

- The Latin Extended subset covers letters that Geist lacks, such as ĭ (U+012D). The Thai
  subset covers the Thai block (U+0E01-0E5B).
- Each file's `unicode-range` is copied from Google's CSS for the same build. Geist has no
  range, so it applies to every character it covers.
- To upgrade a font, download the new file, replace it, update this table, and check that
  the sample on `/dev/components` still renders.
