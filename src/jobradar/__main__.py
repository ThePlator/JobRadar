"""CLI entry point. Commands mirror the LLD; each is wired up in the release noted in PLAN.md."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from jobradar import __version__, logs
from jobradar.config import ConfigError, Settings, load_settings

app = typer.Typer(
    name="jobradar",
    help="Watch job channels, rank postings, build tailored resumes, track it all in Notion.",
    no_args_is_help=True,
    add_completion=False,
)

CONFIG_PATH = Path(os.environ.get("JOBRADAR_CONFIG", "config/config.yaml"))
EXAMPLES = {
    Path("config.example.yaml"): CONFIG_PATH,
    Path("profile.example.yaml"): CONFIG_PATH.parent / "profile.yaml",
    Path(".env.example"): Path(".env"),
}


def _not_implemented(release: str) -> None:
    typer.secho(f"Not implemented yet (planned for {release}, see PLAN.md).", fg="yellow", err=True)
    raise typer.Exit(code=1)


def _fail(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _settings() -> Settings:
    try:
        return load_settings(CONFIG_PATH)
    except ConfigError as e:
        _fail(str(e))
        raise  # unreachable; keeps type checkers happy


def _run(coro: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(coro)
    except ConfigError as e:
        _fail(str(e))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None


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
    """Copy example configs, log in to Telegram and check the Notion database."""
    for example, target in EXAMPLES.items():
        if target.exists():
            typer.echo(f"✓ {target} exists")
        elif example.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example, target)
            typer.secho(f"+ created {target} from {example}: edit it before continuing", fg="cyan")
    settings = _settings()

    async def setup() -> None:
        from jobradar.sinks import notion
        from jobradar.sources.telegram import make_client, protect_session

        settings.require("telegram")
        client = make_client(settings)
        typer.echo("Telegram login (a code is sent to your Telegram app; nothing is posted):")
        await client.start()  # prompts for phone, code and 2FA password if needed
        me = await client.get_me()
        await client.disconnect()
        protect_session()
        typer.secho(f"✓ Telegram logged in as {getattr(me, 'username', None) or me.id}", fg="green")

        sink = await notion.connect(settings)
        await sink.aclose()
        typer.secho("✓ Notion database found and matches the template", fg="green")

    _run(setup())
    typer.echo("Next: add your chats to config (see `jobradar chats`), then `jobradar run`.")


@app.command()
def run() -> None:
    """Start sources, workers and the scheduler (Ctrl+C to stop)."""
    from jobradar import app as application

    logs.setup()
    _run(application.run(_settings()))


@app.command()
def chats() -> None:
    """List Telegram chats the account can read, with ids to paste into config."""
    settings = _settings()

    async def show() -> None:
        from jobradar.sources.telegram import list_chats, make_client

        client = make_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise ConfigError("Not logged in to Telegram. Run `jobradar init` first.")
            rows = await list_chats(client)
        finally:
            await client.disconnect()
        typer.echo(f"{'id':>16}  {'kind':8} title")
        for peer_id, kind, title in rows:
            typer.echo(f"{peer_id:>16}  {kind:8} {title}")
        typer.echo("\nPut ids (or @usernames) under sources.telegram.chats in config.yaml.")

    _run(show())


@app.command()
def add(url: Annotated[str, typer.Argument(help="Job link to process.")]) -> None:
    """Process one link manually and sync it to Notion."""
    from jobradar import app as application
    from jobradar.db.models import Platform
    from jobradar.pipeline.ingest import make_emit
    from jobradar.sources.base import IncomingMessage

    settings = _settings()
    logs.setup()

    async def go() -> None:
        settings.require("notion")
        repo, queue = application.open_store()
        msg = IncomingMessage(
            platform=Platform.MANUAL,
            chat_id="cli",
            chat_title="Added by hand",
            message_id=str(time.time_ns()),
            text=url,
            urls=[url],
            posted_at=datetime.now(UTC),
        )
        await make_emit(repo, queue)(msg)
        await application.drain(settings)

    _run(go())
    typer.secho("✓ processed; check your Notion Inbox", fg="green")


@app.command()
def extract(
    missing: Annotated[
        bool, typer.Option("--missing", help="Queue every listed job that has not been read yet.")
    ] = False,
) -> None:
    """Queue page fetch + LLM extraction for jobs listed before extraction existed."""
    from jobradar import app as application
    from jobradar.pipeline.task_types import FETCH

    if not missing:
        _fail("Nothing to do: pass --missing to read the jobs that were only listed.")
    settings = _settings()
    if not application.extraction_enabled(settings):
        _fail("Add your LLM key to .env first: " + ", ".join(settings.missing("llm")))
    repo, queue = application.open_store()
    jobs = repo.jobs_missing_extraction()
    for job_id in jobs:
        queue.enqueue(FETCH, job_id)
    limits = settings.config.llm
    minutes = len(jobs) / limits.requests_per_minute
    typer.echo(
        f"Queued {len(jobs)} job(s). `jobradar run` works through them at about "
        f"{limits.requests_per_minute:g} per minute (~{minutes:.0f} min), within the "
        f"₹{limits.daily_budget_inr:g}/day budget."
    )


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
    """Re-queue failed tasks; they run on the next `jobradar run`."""
    from jobradar import app as application

    if not failed:
        _fail("Nothing to do: pass --failed to re-queue failed tasks.")
    _, queue = application.open_store()
    count = queue.requeue_failed()
    typer.echo(f"Re-queued {count} failed task(s).")


@app.command()
def doctor() -> None:
    """Check keys, Tectonic, Playwright, Notion properties and Drive access."""
    _not_implemented("v0.4")


@app.command()
def stats(days: Annotated[int, typer.Option(help="Window in days.")] = 7) -> None:
    """Posts received, jobs created, reposts merged and likely duplicates."""
    from jobradar import app as application
    from jobradar.stats import build_report, render

    repo, _ = application.open_store()
    typer.echo(render(build_report(repo, days)))


if __name__ == "__main__":
    app()
