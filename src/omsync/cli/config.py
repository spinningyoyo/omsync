"""config show and config set: read and write omsync.toml from the terminal."""

from typing import Annotated

import typer

from omsync.cli.common import fail
from omsync.cli.groups import config_app
from omsync.core.settings import setting_sources, update_settings


@config_app.command("show")
def config_show() -> None:
    """Show every setting: its name, value, and where it comes from."""
    try:
        rows = setting_sources()
    except ValueError as exc:
        fail(f"config file: {exc}")
    width = max(len(name) for name, _value, _source in rows)
    for name, value, source in rows:
        typer.echo(f"  {name:<{width}}  {value}  ({source})")


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="The setting to change.")],
    value: Annotated[str, typer.Argument(help="The new value.")],
) -> None:
    """Write one setting to omsync.toml. Creates the file if it does not exist."""
    try:
        path = update_settings({key: value})
    except ValueError as exc:
        fail(str(exc))
    typer.echo(f"{key} = {value}")
    typer.echo(f"Written to {path}")
    typer.echo("Restart the server to apply this change.")
