"""CLI entry point. Commands mirror the LLD; each is wired up in the release noted in PLAN.md."""

from typing import Annotated

import typer

from jobradar import __version__

app = typer.Typer(
    name="jobradar",
    help="Watch job channels, rank postings, build tailored resumes, track it all in Notion.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_implemented(release: str) -> None:
    typer.secho(f"Not implemented yet (planned for {release}, see PLAN.md).", fg="yellow", err=True)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"jobradar {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """JobRadar command-line interface."""


@app.command()
def init() -> None:
    """Copy example configs, log in to Telegram, run Google OAuth, check the Notion schema."""
    _not_implemented("v0.1")


@app.command()
def run() -> None:
    """Start sources, workers and the scheduler."""
    _not_implemented("v0.1")


@app.command()
def chats() -> None:
    """List Telegram chats the account can read, with ids to paste into config."""
    _not_implemented("v0.1")


@app.command()
def add(url: Annotated[str, typer.Argument(help="Job link to process.")]) -> None:
    """Process one link manually."""
    _not_implemented("v0.1")


@app.command()
def resume(
    job_id: Annotated[str, typer.Argument(help="Job ID from Notion.")],
    template: Annotated[str | None, typer.Option(help="Template name, e.g. modern.")] = None,
) -> None:
    """Rebuild a resume now (new version)."""
    _not_implemented("v0.3")


@app.command()
def retry(
    failed: Annotated[bool, typer.Option("--failed", help="Re-queue failed tasks.")] = False,
) -> None:
    """Re-queue failed tasks."""
    _not_implemented("v0.1")


@app.command()
def doctor() -> None:
    """Check keys, Tectonic, Playwright, Notion properties and Drive access."""
    _not_implemented("v0.4")


@app.command()
def stats(days: Annotated[int, typer.Option(help="Window in days.")] = 7) -> None:
    """Jobs seen, unique, hidden, shortlisted, applied and LLM spend."""
    _not_implemented("v0.4")


if __name__ == "__main__":
    app()
