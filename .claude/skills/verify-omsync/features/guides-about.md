# Guides and About

Guides are short walkthroughs of each page; About shows the version and system checks.

## Sub-features

- guides: `/settings/guides`, "Start here", "Previous and next guides".
- about: `/settings/about`: version and checks.

## How to get to it (user POV)

- The top bar's Guides, or Settings → Guides; Settings → About.

## Driving it with verify.py

Preconditions: any mode.

- Guides: open, step with next and previous; each guide's images load (no broken image).
- About: expect the version text.

## Gotchas

- Guide screenshots are committed images; a page change can leave them out of date. Report a
  mismatch, do not regenerate them without the owner.
