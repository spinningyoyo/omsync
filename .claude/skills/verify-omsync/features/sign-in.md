# Sign-in

Opening omsync shows the sign-in page. In a ledger, Sign in runs the computer's owner check; in the
demo, Explore the demo signs in with no check. Signing out, 12 idle hours or a server restart end
the session.

## Sub-features

- sign-in: Sign in passes the owner check and lands on Home.
- demo-sign-in: Explore the demo, in demo mode only.
- sign-out: the user menu ends the session; a protected page then redirects to /sign-in.
- scene: the nursery scene on the sign-in page; "Water the scene" changes its stage.

## How to get to it (user POV)

- Open the app at `/` while signed out; it redirects to `/sign-in`.
- The user menu at the foot of the rail (the ⋮ button) has the sign-out item.

## Driving it with verify.py

Preconditions: `drive` has already signed in. To test sign-in itself, clear cookies first.

- Signed-out redirect: `page.context.clear_cookies(); page.goto(base + "/transactions")`, then
  `expect(page).to_have_url(re.compile("/sign-in"))`.
- Sign in (ledger): `page.get_by_role("button", name="Sign in", exact=True).click()`, then expect
  the URL to leave `/sign-in` and the heading "Home".
- Explore the demo (`launch --mode demo`): the button "Explore the demo"; the Demo pill then shows
  in the top bar.
- Scene: `page.get_by_role("button", name="Water the scene").click()`; the stage label changes.

## Gotchas

- The owner check is a stand-in in ledger mode and always passes. A refused owner check cannot be
  driven; say so.
- `drive` signs in before the script, so a script that tests sign-in must clear cookies first.
