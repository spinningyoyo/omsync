"""Reset demo: put the made-up data back to its first state. Exists only in demo mode."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.post("/demo/reset")
def reset_demo(request: Request) -> RedirectResponse:
    state = request.app.state
    if state.demo is None:
        raise HTTPException(status_code=404)
    state.demo.reset(state.engine, state.settings.data_dir)
    # Home says the reset happened (layouts/shell.html), since the page alone looks unchanged.
    return RedirectResponse("/?demo=reset", status_code=303)
