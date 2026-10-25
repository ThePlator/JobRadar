"""CLI entry point. Commands mirror the LLD; each is wired up in the release noted in PLAN.md."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from jobradar import __version__, logs
from jobradar.config import ConfigError, Settings, load_settings

if TYPE_CHECKING:
    from jobradar.services.context import Ctx

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


def _ctx(settings: Settings) -> Ctx:
    """The owner, now, on the local database: what the read commands pass to the services."""
    from jobradar import app as application
    from jobradar.services.context import Ctx

    repo, _ = application.open_store()
    return Ctx(repo, settings, datetime.now(UTC))


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
    """Copy example configs, log in to Telegram and check the Notion database (each if on)."""
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

        if settings.config.sources.telegram.enabled:
            client = make_client(settings)
            typer.echo("Telegram login (a code is sent to your Telegram app; nothing is posted):")
            await client.start()  # prompts for phone, code and 2FA password if needed
            me = await client.get_me()
            await client.disconnect()
            protect_session()
            who = getattr(me, "username", None) or me.id
            typer.secho(f"✓ Telegram logged in as {who}", fg="green")
        else:
            typer.echo("Telegram is off (sources.telegram.enabled: false): no login needed.")

        if not settings.config.notion.enabled:
            typer.echo("Notion is off (notion.enabled: false): the dashboard is your tracker.")
            return
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
    """Process one link by hand (it shows on the dashboard, and in Notion when that's on)."""
    from jobradar import app as application
    from jobradar.services.intake import add_link

    settings = _settings()
    logs.setup()

    async def go() -> None:
        if settings.config.notion.enabled:
            settings.require("notion")
        await add_link(_ctx(settings), url)
        await application.drain(settings)

    _run(go())
    where = "your Notion Inbox" if settings.config.notion.enabled else "the dashboard"
    typer.secho(f"✓ processed; check {where}", fg="green")


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
    again: Annotated[
        str | None,
        typer.Option(
            "--again",
            help="Read again every job from this source, as shown in Notion's Source column "
            '(e.g. "Workday · Accenture"); jobs you moved in Notion are left alone.',
        ),
    ] = None,
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
    if not missing and again is None:
        _fail("Nothing to do: pass --missing, or --again with a source name.")
    settings = _settings()
    repo, queue = application.open_store()
    jobs = repo.jobs_from_source(again) if again else repo.jobs_missing_extraction()
    if again and not jobs:
        _fail(f"No jobs from '{again}' to read again (check the name in Notion's Source column).")
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
    job_id: Annotated[
        str | None,
        typer.Argument(help="Job ID from Notion's Job ID column (the first 8 characters do)."),
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Use profile.yaml as written: no tailoring.")
    ] = False,
    template: Annotated[
        str | None, typer.Option(help="Template name or path to a .tex.j2 file.")
    ] = None,
) -> None:
    """Build a resume for a job now (a new version), or with no job, preview your profile."""
    import json

    from jobradar.profile import load_profile, resume_problems
    from jobradar.queue.tasks import PermanentError, RetryableError
    from jobradar.resume import ats
    from jobradar.resume.build import fit_to_pages
    from jobradar.resume.compile import CompileError
    from jobradar.resume.render import content_from_profile, render
    from jobradar.resume.validate import primary_ids
    from jobradar.services import resumes as resumes_svc
    from jobradar.services.errors import NotFound, Refused

    settings = _settings()
    out = settings.config.storage.local_dir
    if job_id is None:
        profile = load_profile(settings.config.matching.profile_path)
        if problems := resume_problems(profile):
            _fail("profile.yaml: " + "; ".join(problems))
        pdf = out / "profile_preview.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        content = content_from_profile(profile)
        name = template or settings.config.resume.template
        try:
            render(content, name)
        except ValueError as e:
            _fail(str(e))
        limit = settings.config.resume.max_pages

        async def preview() -> None:
            try:
                pages, printed, dropped = await fit_to_pages(
                    content, name, pdf, limit, primary_ids(profile)
                )
            except CompileError as e:
                _fail(f"{e}\nThe .tex is at {pdf.with_suffix('.tex')}.")
                raise
            typer.echo(f"{pdf} ({pages} page(s)); .tex next to it.")
            _ats_report(ats.check(pdf, printed, [], profile).as_record())
            if dropped:
                typer.echo(
                    f"Left out to fit {limit} page(s), least relevant first: "
                    f"{', '.join(dropped)} (tailored resumes choose per job)."
                )

        _run(preview())
        return

    ctx = _ctx(settings)
    try:
        job = resumes_svc.find_job(ctx, job_id)
    except (NotFound, Refused) as e:
        _fail(str(e))
        raise

    async def build() -> None:
        try:
            artifact = await resumes_svc.build_now(ctx, job, tailored=not plain, template=template)
        except PermanentError as e:
            _fail(str(e))
            raise
        except RetryableError as e:
            _fail(f"Gemini is busy right now ({str(e).splitlines()[0][:120]}). Try again in a "
                  "minute, or use --plain.")  # fmt: skip
            raise
        record = json.loads(artifact.content_json or "{}")
        typer.echo(f"{artifact.local_path} (v{artifact.version}, {record.get('pages')} page(s), "
                   f"{'tailored' if record.get('tailored') else 'as written'})")  # fmt: skip
        for fixed in record.get("fixed", []):
            typer.secho(f"  kept your wording: {fixed}", dim=True)
        _ats_report(record.get("ats") or {})
        typer.echo("Notion's Resume column updates when `jobradar run` is running.")

    _run(build())


def _ats_report(report: dict[str, Any]) -> None:
    """What hiring software would read from the PDF: problems, and the job's skills on it."""
    if problems := report.get("problems"):
        for problem in problems:
            typer.secho(f"  hiring software: {problem}", fg="yellow")
        typer.secho("  These come from the template, not your profile: fix them in the .tex.j2.",
                    dim=True)  # fmt: skip
    else:
        typer.secho("  ✓ reads cleanly as text: email, phone, dates and words intact", fg="green")
    on, off = report.get("skills_on_resume") or [], report.get("skills_not_on_resume") or []
    if on or off:
        typer.echo(f"  The job's skills you have: {len(on)} of {len(on) + len(off)} on the page"
                   + (f"; not printed: {', '.join(off)}" if off else ""))  # fmt: skip


@app.command()
def retry(
    failed: Annotated[bool, typer.Option("--failed", help="Re-queue failed tasks.")] = False,
) -> None:
    """Re-queue failed tasks; they run on the next `jobradar run`."""
    from jobradar.services.tasks import retry_failed

    if not failed:
        _fail("Nothing to do: pass --failed to re-queue failed tasks.")
    count = retry_failed(_ctx(_settings()))
    typer.echo(f"Re-queued {count} failed task(s).")


@app.command()
def doctor() -> None:
    """Check keys, Tectonic, Playwright, Notion properties and Drive access."""
    _not_implemented("v0.4")


boards_app = typer.Typer(
    help="Company job boards you watch (sources.boards in config).", no_args_is_help=False
)
app.add_typer(boards_app, name="boards")


@boards_app.callback(invoke_without_command=True)
def boards(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Read every board once now (the first time records a baseline)."
        ),
    ] = False,
) -> None:
    """Company job boards you watch (sources.boards in config), and how each is doing."""
    if ctx.invoked_subcommand is not None:
        return
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
    ats_names = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
                 "smartrecruiters": "SmartRecruiters", "workday": "Workday",
                 "oracle": "Oracle"}  # fmt: skip
    width = max(len(ref.label.split(" · ", 1)[1]) for ref in config.companies) + 2
    typer.echo(f"  {'':<{width}}{'postings':>8}  {'passed on':>9}  {'last read (UTC)':<16}  status")
    for ats, title in ats_names.items():
        refs = [ref for ref in config.companies if ref.ats == ats]
        if not refs:
            continue
        typer.secho(title, bold=True)
        for ref in refs:
            b = repo.get_board(ref.key)
            if b.skip_until:
                state = f"not found 3 times: check the name (retry {b.skip_until[:10]})"
            elif b.last_error:
                state = b.last_error[:60]
            elif b.seen_json is None:
                state = "not read yet"
            else:
                state = "ok"
            postings = str(len(json.loads(b.seen_json))) if b.seen_json else "-"
            last = (b.last_ok or "")[:16].replace("T", " ")
            name = ref.label.split(" · ", 1)[1]
            typer.echo(f"  {name:<{width}}{postings:>8}  {b.new_total:>9}  {last:<16}  {state}")
            if b.latest_json:
                newest = json.loads(b.latest_json)
                verdict = newest["verdict"]
                mark = "✓ passed" if verdict == "passed" else f"skipped: {verdict}"
                where = f" · {newest['where'][:30]}" if newest.get("where") else ""
                passed = verdict == "passed"
                typer.secho(
                    f"    latest: {newest['title'][:60]}{where}  ({mark})",
                    fg="green" if passed else None,
                    dim=not passed,
                )


@boards_app.command("add")
def boards_add(
    board: Annotated[
        str,
        typer.Argument(
            help="A link to the board (https://job-boards.greenhouse.io/stripe, or a Workday or "
            "Oracle careers link), or the hiring system followed by the board name: "
            "greenhouse stripe"
        ),
    ],
    board_name: Annotated[
        str | None, typer.Argument(help="The board name, when the first argument is the system.")
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="How it shows in Notion and emails.")
    ] = None,
) -> None:
    """Add a board to config.yaml, after checking it exists and what it lists for you."""
    from jobradar.config import _ATS_NAMES, BoardRef, board_from_link, load_config
    from jobradar.sources.boards.base import BoardGone, BoardUnavailable
    from jobradar.sources.boards.config_edit import EditError, add_board
    from jobradar.sources.boards.filters import why_skipped
    from jobradar.sources.boards.readers import READERS
    from jobradar.sources.boards.watcher import USER_AGENT

    try:
        if board_name is None:
            ats, slug = board_from_link(board)
        else:
            ats, slug = board.casefold(), board_name
        ref = BoardRef.model_validate({"ats": ats, "company": slug, "name": name})
    except ValueError as e:
        _fail(
            f"{e}\nUse a board link, or one of {', '.join(_ATS_NAMES)} and the board name, "
            "e.g. `jobradar boards add greenhouse stripe --name Stripe`."
        )
        raise
    slug = ref.label.split(" · ", 1)[1]
    label = name or slug.replace("-", " ").title()
    try:
        before = CONFIG_PATH.read_text()
        after = add_board(before, ref.ats, ref.company, label)
    except (OSError, EditError) as e:
        _fail(str(e))
        raise

    async def read() -> None:
        import httpx

        async with httpx.AsyncClient(
            timeout=20, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        ) as client:
            try:
                postings = await READERS[ref.ats](client, ref.company)
            except BoardGone:
                _fail(f"No {_ATS_NAMES[ref.ats]} board called '{ref.company}' (it answered 404).")
            except BoardUnavailable as e:
                _fail(f"Couldn't read the board right now ({e}); try again in a minute.")
        passing = [p for p in postings if why_skipped(p.title, p.location, p.level) is None]
        typer.echo(
            f"{_ATS_NAMES[ref.ats]} · {label}: {len(postings)} postings now, "
            f"{len(passing)} pass your filters (India or remote, tech, not senior)."
        )
        for p in passing[:5]:
            typer.echo(f"  {p.title[:70]} · {p.location[:40]}")

    _run(read())
    CONFIG_PATH.write_text(after)
    try:
        load_config(CONFIG_PATH)
    except ConfigError as e:
        CONFIG_PATH.write_text(before)
        _fail(f"Left config.yaml unchanged: {e}")
    typer.secho(f"Added to {CONFIG_PATH} under {ref.ats}.", fg="green")
    typer.echo(
        "Restart `jobradar run` to start watching it. Its first read records what's there "
        "now: you're alerted only about postings that appear after that."
    )


@boards_app.command("remove")
def boards_remove(
    board: Annotated[str, typer.Argument(help="Its name, board name, or link.")],
    ats: Annotated[
        str | None, typer.Option("--ats", help="Hiring system, if the name is in several.")
    ] = None,
) -> None:
    """Stop watching a board: removes its line from config.yaml."""
    from jobradar.config import load_config
    from jobradar.sources.boards.config_edit import EditError, remove_board

    try:
        before = CONFIG_PATH.read_text()
        after, section, name = remove_board(before, board, ats)
    except (OSError, EditError, ValueError) as e:
        _fail(str(e))
        raise
    CONFIG_PATH.write_text(after)
    try:
        load_config(CONFIG_PATH)
    except ConfigError as e:
        CONFIG_PATH.write_text(before)
        _fail(f"Left config.yaml unchanged: {e}")
    typer.secho(f"Removed {name} ({section}) from {CONFIG_PATH}.", fg="green")
    typer.echo("Restart `jobradar run` to stop reading it. Jobs it found stay in Notion.")


pages_app = typer.Typer(
    help="Web pages you watch for announcements, such as hiring-drive pages (sources.pages).",
    no_args_is_help=False,
)
app.add_typer(pages_app, name="pages")


def _page_watcher(settings: Settings) -> Any:
    from jobradar import app as application
    from jobradar.pipeline.fetch import Fetcher
    from jobradar.pipeline.ingest import make_emit
    from jobradar.sources.pages.watcher import PageWatcher

    repo, queue = application.open_store()
    return PageWatcher(
        settings.config.sources.pages, repo, queue, make_emit(repo, queue), Fetcher()
    )


@pages_app.callback(invoke_without_command=True)
def pages(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Read every page once now (the first time records a baseline)."
        ),
    ] = False,
) -> None:
    """Pages you watch (sources.pages in config), and how each is doing."""
    if ctx.invoked_subcommand is not None:
        return
    settings = _settings()
    watch = settings.config.sources.pages.watch
    if not watch:
        _fail("No pages yet: `jobradar pages add <name> <url>` adds one.")
    watcher = _page_watcher(settings)
    if check:

        async def read_all() -> None:
            try:
                for name, url in watch.items():
                    await watcher.check_page(name, url)
            finally:
                await watcher.fetcher.aclose()

        _run(read_all())
        typer.echo("Read every page. New announcements (if any) are queued for `jobradar run`.\n")
    width = max(len(name) for name in watch) + 2
    typer.echo(f"  {'':<{width}}{'posts':>5}  {'last read (UTC)':<16}  {'last change':<16}  state")
    for name, url in watch.items():
        p = watcher.repo.get_watched_page(url, name)
        state = p.state.replace("_", " ")
        if p.state == "backing_off" and p.wait_until:
            state += f" until {p.wait_until[11:16]}"
        if p.state == "new":
            state = "not read yet"
        last = (p.last_ok or "")[:16].replace("T", " ")
        changed = (p.last_changed or "")[:16].replace("T", " ")
        typer.echo(f"  {name:<{width}}{p.posts_total:>5}  {last:<16}  {changed:<16}  {state}")
        if p.last_error:
            typer.secho(f"    {p.last_error[:90]}", dim=True)
        typer.secho(f"    {url}", dim=True)


@pages_app.command("add")
def pages_add(
    name: Annotated[str, typer.Argument(help="How it shows in Notion and emails.")],
    url: Annotated[str, typer.Argument(help="The page's address (https://…).")],
) -> None:
    """Watch a page: checks robots.txt, reads it once, then adds it to config.yaml."""
    from jobradar.config import load_config
    from jobradar.pipeline.fetch import Fetcher
    from jobradar.sources.boards.config_edit import EditError
    from jobradar.sources.pages.config_edit import add_page

    url = url.strip().split("#")[0]
    if not url.startswith(("https://", "http://")):
        _fail("Give the full address, starting with https://")
    try:
        before = CONFIG_PATH.read_text()
        after = add_page(before, name, url)
    except (OSError, EditError) as e:
        _fail(str(e))
        raise

    async def read() -> None:
        fetcher = Fetcher()
        try:
            page = await fetcher.fetch(url, links=True)
        finally:
            await fetcher.aclose()
        if page.text is None:
            _fail(f"Can't watch it: {page.note or 'no readable text'}.")
        lines = [line for line in (page.text or "").splitlines() if line.strip()]
        typer.echo(f"{name}: {len(lines)} lines of text now. A sample:")
        for line in lines[:5]:
            typer.echo(f"  {line[:90]}")

    _run(read())
    CONFIG_PATH.write_text(after)
    try:
        load_config(CONFIG_PATH)
    except ConfigError as e:
        CONFIG_PATH.write_text(before)
        _fail(f"Left config.yaml unchanged: {e}")
    typer.secho(f"Added to {CONFIG_PATH} under sources.pages.watch.", fg="green")
    typer.echo(
        "Restart `jobradar run` to start watching it. Its first read records what's there "
        "now: you're alerted only about what's added after that."
    )


@pages_app.command("remove")
def pages_remove(name: Annotated[str, typer.Argument(help="Its name or address.")]) -> None:
    """Stop watching a page: removes its line from config.yaml."""
    from jobradar.config import load_config
    from jobradar.sources.boards.config_edit import EditError
    from jobradar.sources.pages.config_edit import remove_page

    try:
        before = CONFIG_PATH.read_text()
        after, removed = remove_page(before, name)
    except (OSError, EditError) as e:
        _fail(str(e))
        raise
    CONFIG_PATH.write_text(after)
    try:
        load_config(CONFIG_PATH)
    except ConfigError as e:
        CONFIG_PATH.write_text(before)
        _fail(f"Left config.yaml unchanged: {e}")
    typer.secho(f"Removed {removed} from {CONFIG_PATH}.", fg="green")
    typer.echo("Restart `jobradar run` to stop reading it. Jobs it found stay in Notion.")


@pages_app.command("resume")
def pages_resume(name: Annotated[str, typer.Argument(help="Its name or address.")]) -> None:
    """Watch a paused or blocked page again from its next turn."""
    settings = _settings()
    watch = settings.config.sources.pages.watch
    url = watch.get(name) or next(
        (u for n, u in watch.items() if name.casefold() in (n.casefold(), u)), None
    )
    if url is None:
        _fail(f"No page '{name}' in config.yaml; `jobradar pages` lists them.")
        raise typer.Exit(2)
    if _page_watcher(settings).resume(url):
        typer.secho(f"{name} will be read again on its next turn.", fg="green")
    else:
        typer.echo(f"{name} isn't paused or blocked; nothing to do.")


@app.command()
def freehire(
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Read the search once now (the first time records a baseline)."
        ),
    ] = False,
) -> None:
    """freehire.me's job search (sources.freehire): how it's doing."""
    import json

    from jobradar import app as application
    from jobradar.pipeline.ingest import make_emit
    from jobradar.sources.boards.freehire import KEY, FreehireWatcher

    settings = _settings()
    config = settings.config.sources.freehire
    repo, queue = application.open_store()
    if check:
        watcher = FreehireWatcher(config, repo, queue, make_emit(repo, queue))

        async def read() -> None:
            try:
                passed = await watcher.check()
            finally:
                await watcher.aclose()
            typer.echo(f"Read the search; {passed} new posting(s) queued for `jobradar run`.\n")

        _run(read())
    board = repo.get_board(KEY)
    where = ", ".join(config.countries)
    typer.echo(
        f"freehire: {'on' if config.enabled else 'off (sources.freehire.enabled)'} · "
        f"{where} · {', '.join(config.seniority)} · every {config.every_minutes} min"
    )
    if board.seen_json is None:
        typer.echo("  not read yet")
        return
    last = (board.last_ok or "")[:16].replace("T", " ")
    typer.echo(f"  last read (UTC) {last} · {board.jobs_on_board} in the search · "
               f"{board.new_total} passed on so far")  # fmt: skip
    if board.last_error:
        typer.secho(f"  {board.last_error[:100]}", fg="yellow")
    if board.latest_json:
        newest = json.loads(board.latest_json)
        mark = "✓ passed" if newest["verdict"] == "passed" else f"skipped: {newest['verdict']}"
        line = f"  latest: {newest['title'][:60]} · {newest['where'][:30]}  ({mark})"
        typer.secho(line, dim=True)


@app.command()
def alerts(
    test: Annotated[
        bool, typer.Option("--test", help="Send yourself one test email, even in dry run.")
    ] = False,
) -> None:
    """Email alert tools."""
    from jobradar.services.errors import Refused
    from jobradar.services.notify import send_test_alert

    if not test:
        _fail("Nothing to do: pass --test to send yourself a test email.")
    try:
        outcome = send_test_alert(_ctx(_settings()))
    except Refused as e:
        _fail(str(e))
        raise
    typer.secho(f"✓ {outcome}", fg="green")


@app.command()
def digest(
    now: Annotated[
        bool, typer.Option("--now", help="Send the digest for the current window right away.")
    ] = False,
) -> None:
    """Morning and evening summary email (sent automatically by `jobradar run`)."""
    from jobradar.services.errors import Refused
    from jobradar.services.notify import send_digest_now

    if not now:
        _fail("Nothing to do: pass --now to send the digest for the current window.")
    try:
        outcome = asyncio.run(send_digest_now(_ctx(_settings())))
    except Refused as e:
        _fail(str(e))
        raise
    typer.secho(f"✓ {outcome}", fg="green")


@app.command()
def followups(
    now: Annotated[
        bool, typer.Option("--now", help="Email the follow-ups that are due right away.")
    ] = False,
) -> None:
    """Jobs you set to Applied: days without a reply and when a follow-up draft is due."""
    from jobradar.services.errors import Refused
    from jobradar.services.notify import followups as due
    from jobradar.services.notify import send_followups_now
    from jobradar.sinks.followups import MAX_REMINDERS

    settings = _settings()
    notify = settings.config.notify
    ctx = _ctx(settings)
    rows = due(ctx)
    if not rows:
        typer.echo("No job has Status Applied in Notion yet (JobRadar reads it every 2 minutes "
                   "while `jobradar run` is running).")  # fmt: skip
        return
    typer.echo(f"Follow-up drafts come {notify.followup_after_days} days after Applied (or your "
               f"Followed Up date) with no reply, at most {MAX_REMINDERS} per job.")  # fmt: skip
    for r in rows:
        quiet = (ctx.now - r.since).days
        when = {
            "done": f"done: {r.sent} follow-ups sent",
            "quiet": f"quiet for {quiet} days: no more reminders",
            "due": "due now",
        }.get(r.state, f"next draft {r.next_at.astimezone(notify.tz):%d %b}")
        role = f"{r.job.role or 'Role not stated'} @ {r.job.company or '?'}"
        typer.echo(f"  {r.job.id[:8]}  {role[:48]:<48}  applied "
                   f"{r.applied.astimezone(notify.tz):%d %b}  {when}")  # fmt: skip
    if not now:
        return
    try:
        outcome = asyncio.run(send_followups_now(ctx))
    except Refused as e:
        _fail(str(e))
        raise
    typer.secho(f"✓ {outcome}", fg="green")


@app.command()
def replies(
    check: Annotated[
        bool, typer.Option("--check", help="Read the inbox now and act on button emails.")
    ] = False,
) -> None:
    """The "I applied" / "Not interested" / "I followed up" buttons in JobRadar's emails."""
    from jobradar import app as application
    from jobradar.queue.tasks import PermanentError, RetryableError
    from jobradar.sinks.company_replies import CompanyReplies
    from jobradar.sinks.email_actions import Actions, ReplyReader

    settings = _settings()
    repo, queue = application.open_store()
    actions = Actions.load(settings, repo)
    if actions is None:
        _fail("Email buttons are off: set notify.email_actions: true and EMAIL_ADDRESS in .env.")
        return
    if missing := settings.missing("email"):
        _fail("Add to .env first: " + ", ".join(missing))
    typer.echo(f"Buttons open an email to {actions.address}. While `jobradar run` is running, "
               "it reads the inbox every 2 minutes and updates the job and Notion.")  # fmt: skip
    if not check:
        typer.echo("Run with --check to read the inbox now.")
        return
    try:
        companies = (CompanyReplies(settings, repo, actions)
                     if settings.config.notify.company_replies else None)  # fmt: skip
        done = asyncio.run(
            ReplyReader(settings, repo, queue, actions, companies=companies).check_now()
        )
    except (PermanentError, RetryableError) as e:
        _fail(str(e))
    for line in done:
        typer.secho(f"  ✓ {line}", fg="green")
    typer.echo(f"{len(done)} button email(s) acted on. Notion updates when `jobradar run` runs."
               if done else "No new button emails in the last 7 days.")  # fmt: skip


@app.command()
def skills(days: Annotated[int, typer.Option(help="Look back this many days.")] = 7) -> None:
    """Skills your good matches asked for that your profile doesn't list."""
    from jobradar.profile import load_profile, skill_warnings
    from jobradar.services import skills as skills_svc

    settings = _settings()
    profile = load_profile(settings.config.matching.profile_path)
    for warning in skill_warnings(profile):
        typer.secho(f"profile.yaml: {warning}", fg="yellow")
    ctx = _ctx(settings)
    alert_above = ctx.good
    gaps, looked_at = skills_svc.gaps(ctx, profile, days)
    typer.echo(f"{looked_at} good matches (score {alert_above}+) in the last {days} days. "
               "Skills they name that your profile doesn't list:")  # fmt: skip
    if not gaps:
        typer.echo("  none named by two or more jobs.")
        return
    width = max(len(g.name) for g in gaps)
    for g in gaps:
        typer.echo(f"  {g.name:<{width}}  {'█' * min(g.jobs, 40)} {g.jobs}")
    typer.echo("Already know one? Add it to skills in profile.yaml, so matching counts it.")


@app.command()
def sources() -> None:
    """Each chat, board or page you applied from, and how many reached the next round."""
    from jobradar.by_source import render
    from jobradar.services.applications import sources_report

    typer.echo(render(sources_report(_ctx(_settings()))))


@app.command()
def web() -> None:
    """Your jobs and applications on a page: http://127.0.0.1:8080 (Ctrl+C to stop)."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        _fail(
            "The dashboard needs the web extra: uv sync --extra web "
            "(or pip install 'jobradar-agent[web]')"
        )
    from jobradar import app as application
    from jobradar.web.server import NoFreePort, serve

    logs.setup()
    settings = _settings()
    repo, queue = application.open_store()
    try:
        _run(serve(settings, repo, queue))
    except NoFreePort as e:
        _fail(str(e))


@app.command("saas-import")
def saas_import(
    source: Annotated[
        Path, typer.Option("--from", help="The SQLite database to copy (opened read-only).")
    ] = Path("data/jobradar.db"),
) -> None:
    """Copy your SQLite database into the Postgres in DATABASE_URL (the SaaS). Safe to run
    again: rows already there are kept."""
    from jobradar.db.repo import Repo, connect
    from jobradar.db.transfer import copy, read_only
    from jobradar.services import search

    settings = _settings()
    url = settings.secrets.database_url
    if url is None:
        _fail("Set DATABASE_URL in .env to your Postgres first (see SAASPLAN.md, PR 3).")
        raise typer.Exit(1)
    target = connect(url.get_secret_value())
    if target.dialect.name != "postgresql":
        _fail("DATABASE_URL isn't a Postgres address.")
    try:
        report = copy(read_only(source), target)
    except FileNotFoundError as e:
        _fail(str(e))
        raise
    width = max(len(r.table) for r in report)
    for r in report:
        mark = "✓" if r.ok else "✗"
        counts = f"{r.in_sqlite:>7} in SQLite  {r.in_postgres:>7} in Postgres"
        typer.echo(f"  {mark} {r.table:<{width}}  {counts}")
    indexed = search.refresh(Repo(target), datetime.now(UTC))
    typer.echo(f"  search index built for {indexed} jobs")
    if not all(r.ok for r in report):
        _fail("Some tables have fewer rows in Postgres than in SQLite (see ✗ above).")
    typer.secho(f"✓ copied {source} into Postgres; {source} is unchanged", fg="green")


@app.command()
def stats(days: Annotated[int, typer.Option(help="Window in days.")] = 7) -> None:
    """Posts received, jobs created, reposts merged and likely duplicates."""
    from jobradar.services.stats import report
    from jobradar.stats import render

    typer.echo(render(report(_ctx(_settings()), days)))


if __name__ == "__main__":
    app()
