from jobradar.pipeline.dedupe import fingerprint, job_id_for_url, norm


def test_job_id_is_stable_sha1() -> None:
    assert job_id_for_url("https://acme.com/j/1") == job_id_for_url("https://acme.com/j/1")
    assert len(job_id_for_url("https://acme.com/j/1")) == 40


def test_norm_and_fingerprint() -> None:
    assert norm("Acme Pvt. Ltd. is Hiring!") == "acme"
    assert fingerprint("ACME Private Limited", "Software Engineer - I", "Bengaluru, KA") == (
        "acme|software engineer i|bengaluru ka"
    )
    assert fingerprint(None, "SDE", "Pune") is None
