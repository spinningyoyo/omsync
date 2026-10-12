"""Development pages for the design system: a component gallery at /dev/components.

Nothing here is linked from the app. The gallery shows every component in both themes. Icons
come from the `icon_svg` Jinja global (web/icons.py), so templates need no icon context.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from omsync.core.money import format_cents
from omsync.web.icons import icon_names

router = APIRouter(prefix="/dev")

# Amounts in integer cents, from zero through large negative values.
MONEY_SAMPLES_CENTS = (0, 1, 4550, 123456, -123456, 100000000)
# The vendored set is all of Lucide, so the gallery shows a sample.
ICON_SAMPLE = 48


@router.get("/components", response_class=HTMLResponse)
def components(request: Request) -> HTMLResponse:
    money_rows = [[str(cents), format_cents(cents)] for cents in MONEY_SAMPLES_CENTS]
    transactions = [
        ["2026-10-01", "Corner Bakery", format_cents(-1250)],
        ["2026-10-02", "Transit card", format_cents(-4550)],
        ["2026-10-03", "Refund: hardware store", format_cents(2399)],
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "dev/components.html",
        {
            "icon_names": icon_names()[:ICON_SAMPLE],
            "money_rows": money_rows,
            "transactions": transactions,
        },
    )
