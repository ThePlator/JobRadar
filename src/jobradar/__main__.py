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
    compare: Annotated[
        bool,
        typer.Option(
            "--compare", help="Check the free layers against what the LLM extracted earlier."
        ),
    ] = False,
    refetch: Annotated[
        bool,
        typer.Option(
            "--refetch", help="With --compare: download pages again to read their JobPosting data."
        ),
    ] = False,
    examples: Annotated[
        int, typer.Option(help="With --compare: show this many differences per field.")
    ] = 0,
) -> None:
    """Read jobs listed before extraction existed, or check extraction accuracy."""
    from jobradar import app as application
    from jobradar.pipeline.task_types import FETCH

    if compare:
        from jobradar.extract_check import refetch_pages, render, run_check

        repo, _ = application.open_store()
        if refetch:
            count = asyncio.run(refetch_pages(repo))
            typer.echo(f"Downloaded {count} page(s) again.\n")
        typer.echo(render(run_check(repo), examples))
        return
    if not missing:
        _fail("Nothing to do: pass --missing to read the jobs that were only listed.")
    settings = _settings()
    repo, queue = application.open_store()
    jobs = repo.jobs_missing_extraction()
    for job_id in jobs:
        queue.enqueue(FETCH, job_id)
    note = "`jobradar run` reads them from page data and post labels"
    if application.llm_enabled(settings):
        limits = settings.config.llm
        note += (
            ", asking the LLM only when those miss role or company (at most "
            f"{limits.requests_per_minute:g} per minute, within the "
            f"₹{limits.daily_budget_inr:g}/day budget)"
        )
    typer.echo(f"Queued {len(jobs)} job(s). {note}.")


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
def boards(
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Read every board once now (the first time records a baseline)."
        ),
    ] = False,
) -> None:
    """Company job boards you watch (sources.boards in config), and how each is doing."""
    import json

    from jobradar import app as application
    from jobradar.pipeline.ingest import make_emit
    from jobradar.sources.boards.watcher import BoardWatcher

    settings = _settings()
    config = settings.config.sources.boards
    if not config.companies:
        _fail(
            "No boards yet: add sources.boards.companies to config/config.yaml "
            "(see config.example.yaml)."
        )
    repo, queue = application.open_store()
    if check:
        watcher = BoardWatcher(config, repo, queue, make_emit(repo, queue))

        async def read_all() -> None:
            try:
                for ref in config.companies:
                    await watcher.check_board(ref)
            finally:
                await watcher.aclose()

        _run(read_all())
        typer.echo("Read every board. New postings (if any) are queued for `jobradar run`.\n")
    rows = []
    for ref in config.companies:
        b = repo.get_board(ref.key)
        if b.skip_until:
            state = f"not found 3 times: check the name (retry {b.skip_until[:10]})"
        elif b.last_error:
            state = b.last_error[:60]
        elif b.seen_json is None:
            state = "not read yet"
        else:
            state = "ok"
        worth = 0
        if b.seen_json:
            worth = len(json.loads(b.seen_json))
        rows.append((ref.label, str(worth) if b.seen_json else "-", str(b.new_total),
                     (b.last_ok or "")[:16].replace("T", " "), state))  # fmt: skip
    width = max(len(r[0]) for r in rows)
    typer.echo(
        f"{'Board':<{width}}  {'postings':>8}  {'passed on':>9}  {'last read (UTC)':<16}  status"
    )
    for label, postings, new, last, state in rows:
        typer.echo(f"{label:<{width}}  {postings:>8}  {new:>9}  {last:<16}  {state}")


@app.command()
def alerts(
    test: Annotated[
        bool, typer.Option("--test", help="Send yourself one test email, even in dry run.")
    ] = False,
) -> None:
    """Email alert tools."""
    from jobradar.queue.tasks import PermanentError, RetryableError
    from jobradar.sinks.email_notify import smtp_send, test_email

    if not test:
        _fail("Nothing to do: pass --test to send yourself a test email.")
    settings = _settings()
    if missing := settings.missing("email"):
        _fail("Add to .env first: " + ", ".join(missing))
    msg = test_email(settings)
    try:
        smtp_send(settings, msg)
    except (PermanentError, RetryableError) as e:
        _fail(f"Not sent: {e}")
    mode = (
        "dry run is on: real alerts are saved to data/alerts/ instead"
        if (settings.config.notify.dry_run)
        else "alerts are on"
    )
    typer.secho(f"✓ test email sent to {msg['To']} ({mode})", fg="green")


@app.command()
def digest(
    now: Annotated[
        bool, typer.Option("--now", help="Send the digest for the current window right away.")
    ] = False,
) -> None:
    """Morning and evening summary email (sent automatically by `jobradar run`)."""
    from jobradar import app as application
    from jobradar.queue.tasks import PermanentError, RetryableError
    from jobradar.sinks.digest import Digest

    if not now:
        _fail("Nothing to do: pass --now to send the digest for the current window.")
    settings = _settings()
    if not settings.config.notify.dry_run and (missing := settings.missing("email")):
        _fail("Add to .env first: " + ", ".join(missing))
    repo, queue = application.open_store()
    try:
        outcome = asyncio.run(Digest(settings, repo, queue).send_now())
    except (PermanentError, RetryableError) as e:
        _fail(f"Not sent: {e}")
    typer.secho(f"✓ {outcome}", fg="green")


@app.command()
def stats(days: Annotated[int, typer.Option(help="Window in days.")] = 7) -> None:
    """Posts received, jobs created, reposts merged and likely duplicates."""
    from jobradar import app as application
    from jobradar.stats import build_report, render

    settings = _settings()
    repo, _ = application.open_store()
    typer.echo(render(build_report(repo, days, alert_above=settings.config.scoring.alert_above)))


if __name__ == "__main__":
    app()
