---
status: accepted
---

# Separate Chase personal and Chase Business connectors

## Context

Chase personal and Chase Business share account-download pages, but they are not one stable
browser flow. Their verification pages use different controls, the business flow can ask for the
password again, and current business credit-card downloads require an
`eligibility-indicator=true` query field.

A shared connector previously inferred a `personal` or `business` variant from transient MFA URLs.
That inference was not authoritative: a trusted browser session can skip the recognizable MFA
page. During a live Chase Business failure, the shared connector therefore labeled the run as a
personal sign-in and built a card request without the business-only field. Chrome's retained
network log showed otherwise identical requests for the same account, date range, session, and
security token: the request without the field received 500, while the manual request with the
field received 200.

Provider configuration already stores a connector key. It can express which contract the user
selected without guessing from whichever page Chase happens to show.

## Decision

Register two explicit connectors:

- `chase` owns the personal verification flow and personal request policy.
- `chase_business` owns the business verification flow, including its secondary-password step and
  required credit-card eligibility field.

The classes share statement-page navigation, token acquisition, account matching, and file
retention through a private base class. They do not detect or switch variants at runtime. The
generic connector interface remains independent of provider keys.

A migration changes providers at the `chase_business` institution from connector `chase` to
`chase_business`. New setup uses the business connector directly. Historical sync rows retain the
connector key that actually ran at the time.

## Considered alternatives

### Infer the variant from authentication URLs

Rejected because MFA pages are conditional. A trusted session can omit the URL that inference
needs, which makes a business provider silently take personal behavior.

### Pass the provider key into the connector

Rejected because provider keys are user-facing configuration identities, not bank protocol
variants. It would also couple every connector to orchestration context solely to special-case one
bank.

### Add the eligibility field to every Chase card request

Rejected because the observed requirement belongs to the business flow. Keeping it there avoids
changing a working personal flow without evidence.

Revised on 2026-11-05: a live trace showed Chase's site sends the field on personal card downloads
too, and the personal connector was already repeating it from the site's request. Once that request
was no longer captured, every card request sends the field. The two connectors stay separate.

Revised on 2026-11-07: a live trace showed Chase Business moved to personal's identity check, the
`confirmIdentity` screens, where it offers a code by text or call and asks for no password again.
The check moved into the shared base class. Business still follows its older `simplerAuthOptions`
screen when Chase shows it. That choice is between two Business screens, made from the screen on
the page, not a guess between personal and Business, so the connectors stay separate.

## Consequences

- Authentication and request behavior are deterministic from the configured connector.
- Personal and business selectors can evolve independently while their common download mechanics
  remain shared.
- `chase_business` appears as a separate bank choice when creating a provider.
- Existing providers at the standard `chase_business` institution are migrated automatically.
  A business login configured under another institution must be recreated with the explicit
  business connector.
- Changes to either connector still require live validation against that Chase login; fixture tests
  establish local routing and request construction but cannot prove what Chase currently accepts.
