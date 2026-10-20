from jobradar.pipeline.skill_detail import detail, mention, sentences

HAVE = {"python", "java", "sql"}


def test_sentences_keep_bulleted_lists_together() -> None:
    text = (
        "Proficiency in one or more languages, including – Java – C/C++ – Python\n"
        "Required Skills: - Git - Linux\nApply at https://example.com/apply before the end of October."
    )
    assert sentences(text) == (
        "Proficiency in one or more languages, including: Java, C/C++, Python",
        "Required Skills: Git, Linux",
        "Apply at before the end of October.",
    )


def test_how_a_sentence_asks() -> None:
    def kind(sentence: str, key: str) -> tuple[str, list[str]] | None:
        m = mention(sentence, key, HAVE)
        return (m.kind, m.alternatives) if m else None

    assert kind("Strong systems programming in C++ on Linux", "c++") == ("must", [])
    assert kind("Good knowledge of Python, Java or C++", "c++") == ("covered", ["java", "python"])
    assert kind("Experience with AWS, Azure or GCP", "aws") == (
        "either",
        ["azure", "googlecloud"],
    )
    assert kind("Exposure to AWS is a plus", "aws") == ("nice", [])
    assert kind("Familiarity with AWS services and CI/CD", "aws") == ("nice", [])
    assert kind("Our customers include Uber and AWS.", "aws") is None  # about the company
    assert kind("Build one AWS serverless project to show it.", "aws") is None  # prep advice


def test_detail_sorts_jobs_and_finds_what_exactly() -> None:
    texts = {
        "a": "Must have: hands-on AWS (Lambda, API Gateway, DynamoDB) for serverless backends.",
        "b": "Deploy on AWS.\n- Use S3 and CloudFront for the frontend.",
        "c": "Cloud: AWS or Azure (any one major).",
        "d": "Python, Java or C++.",
        "e": "Nothing about it here.",
    }
    aws = detail("aws", ["a", "b", "c", "e"], texts, HAVE)
    assert [n.kind for n in aws.needs] == ["must", "must", "either", "listed"]
    assert aws.needed == 4 and aws.needs[2].alternatives == ["azure"]
    topics = {t.label: t.jobs for t in aws.topics}
    assert topics["Lambda"] == ["a"] and topics["S3"] == ["b"]  # from the line after, too
    assert topics["API Gateway"] == ["a"] and "EC2" not in topics
    cpp = detail("c++", ["d"], texts, HAVE)
    assert cpp.counts["covered"] == 1 and cpp.needed == 0 and cpp.covered_by == ["java", "python"]
