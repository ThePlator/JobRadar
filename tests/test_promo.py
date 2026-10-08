from datetime import UTC, datetime

from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.ingest import NOTION_UPSERT, PROCESS_MESSAGE, Ingest, make_emit
from jobradar.pipeline.promo import PostContext, different_jobs, looks_like_promo
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import IncomingMessage

PROMO = "https://freshershunt.in/WhatsApp"


def post(mid: str, text: str) -> IncomingMessage:
    return IncomingMessage(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_title="Freshershunt",
        message_id=mid,
        text=text,
        posted_at=datetime(2026, 10, 8, 8, int(mid), tzinfo=UTC),
    )


def ctx(text: str | None, *others: str) -> PostContext:
    return PostContext(text, frozenset(others))


def test_identity_by_other_links_then_titles() -> None:
    # The ad sits next to a different job in every post, even when titles look alike.
    assert different_jobs([ctx("Freshershunt update", "ibm"), ctx("Freshershunt update", "tcs"),
                           ctx("Freshershunt update", "wipro")]) == 3  # fmt: skip
    # A real job reposted sits next to the same promo links each time.
    assert different_jobs([ctx("IBM SE", "whatsapp", "app"), ctx("IBM SE!", "whatsapp", "app"),
                           ctx("IBM SE", "whatsapp")]) == 1  # fmt: skip
    # No other links: fall back to titles, with rewording tolerated.
    assert (
        different_jobs([ctx("IBM ISDL SE 2026 (repost 1)"), ctx("IBM ISDL SE 2026 (repost 2)")])
        == 1
    )
    assert (
        different_jobs(
            [ctx("Infosys hiring Software Engineer"), ctx("TCS hiring Software Engineer")]
        )
        == 2
    )


def test_looks_like_promo() -> None:
    assert looks_like_promo([ctx("a", "j1"), ctx("b", "j2"), ctx("c", "j3")])
    assert not looks_like_promo([ctx("a", "j1"), ctx("b", "j1")])


async def run_all(ingest: Ingest, queue: TaskQueue) -> list[str]:
    while (t := queue.claim([PROCESS_MESSAGE])) is not None:
        await ingest.process_message(t)
        queue.complete(t)
    upserts = []
    while (t := queue.claim([NOTION_UPSERT])) is not None:
        upserts.append(t.key)
        queue.complete(t)
    return upserts


async def test_channel_link_on_three_different_jobs_is_hidden(repo: Repo, queue: TaskQueue) -> None:
    emit = make_emit(repo, queue)
    ingest = Ingest(repo, queue)
    promo_id = job_id_for_url(PROMO)
    await emit(post("1", f"IBM Software Engineer 2026\nhttps://ibm.com/j/1\nJoin: {PROMO}"))
    await emit(post("2", f"TCS Ninja Hiring 2026\nhttps://tcs.com/j/2\nJoin: {PROMO}"))
    first = await run_all(ingest, queue)
    assert promo_id in first  # two different jobs: not proven to be an ad yet

    await emit(post("3", f"Wipro Elite NTH 2026\nhttps://wipro.com/j/3\nJoin: {PROMO}"))
    second = await run_all(ingest, queue)
    promo = repo.get_job(promo_id)
    assert promo is not None and promo.status == JobStatus.HIDDEN
    assert promo_id in second  # one more sync flips its Notion page to Hidden

    await emit(post("4", f"Accenture ASE 2026\nhttps://accenture.com/j/4\nJoin: {PROMO}"))
    third = await run_all(ingest, queue)
    assert promo_id not in third  # hidden links stop syncing
    assert job_id_for_url("https://accenture.com/j/4") in third


async def test_real_job_reposted_stays_visible(repo: Repo, queue: TaskQueue) -> None:
    emit = make_emit(repo, queue)
    ingest = Ingest(repo, queue)
    for mid in ("1", "2", "3", "4"):
        await emit(
            post(mid, f"IBM ISDL Software Engineer 2026 (repost {mid})\nhttps://ibm.com/j/1")
        )
    await run_all(ingest, queue)
    job = repo.get_job(job_id_for_url("https://ibm.com/j/1"))
    assert job is not None and job.status == JobStatus.DISCOVERED


async def test_sweep_hides_ads_stored_earlier(repo: Repo, queue: TaskQueue) -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "-1001")
    assert src.id is not None
    repo.get_or_create_job("ad", PROMO)
    for i, title in enumerate(
        ["IBM SE 2026 hiring", "TCS Ninja Digital 2026", "Wipro Elite NTH 2026"]
    ):
        raw_id, _ = repo.save_raw_message(src.id, str(i), f"2026-10-08T08:0{i}:00.000000Z", title)
        repo.add_job_source("ad", raw_id)
    assert Ingest(repo, queue).sweep_promos() == 1
    ad = repo.get_job("ad")
    assert ad is not None and ad.status == JobStatus.HIDDEN


async def test_sweep_applies_deny_rules_to_old_rows(repo: Repo, queue: TaskQueue) -> None:
    repo.get_or_create_job("tw", "https://x.com/someone")
    repo.get_or_create_job("course", "https://acciojob.com/full-stack-development-courses")
    repo.get_or_create_job("real", "https://acme.com/j/1")
    ingest = Ingest(repo, queue, deny=["acciojob.com/full-stack-development-courses"])
    assert ingest.sweep_promos() == 2
    statuses = {j: getattr(repo.get_job(j), "status", None) for j in ("tw", "course", "real")}
    assert statuses == {"tw": "hidden", "course": "hidden", "real": "discovered"}
