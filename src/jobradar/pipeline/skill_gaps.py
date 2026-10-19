"""Skills you keep missing: across your good matches, the technologies they ask for that
your profile doesn't list, counted per job. Shown once a week in the digest and by
`jobradar skills`.

A job's skills are its stated ones (`skills_required` / `skills_preferred`, when extraction
found them) plus every known technology named in its own page or post. Few jobs state a
skill list, so the text is what makes this useful. Names that are also everyday English
("Go", "Spring", "Express") are only counted from stated skill lists, never from prose.
Synonyms count once ("k8s" is Kubernetes). No LLM call: it's all already stored.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jobradar.clock import to_iso
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.extract import own_post
from jobradar.providers.skills_rule import profile_skill_keys, skill_key, technical
from jobradar.resume.validate import COMMON_TECH, keys_in

# Technology names that are also plain words: only trusted from a stated skill list.
AMBIGUOUS = {
    "go", "spring", "express", "lambda", "excel", "swift", "rust", "ruby", "dart", "helm",
    "spark", "jest", "temporal", "flask", "bash", "dotnet",
}  # fmt: skip
# How a skill is printed (key -> name); anything else prints as found.
NAMES = {
    "aws": "AWS", "azure": "Azure", "googlecloud": "Google Cloud", "kubernetes": "Kubernetes",
    "c++": "C++", "c#": "C#", "go": "Go", "nosql": "NoSQL", "llm": "LLMs", "nlp": "NLP",
    "machinelearning": "Machine learning", "deeplearning": "Deep learning",
    "generativeai": "Generative AI", "computervision": "Computer vision", "pytorch": "PyTorch",
    "tensorflow": "TensorFlow", "scikitlearn": "scikit-learn", "pandas": "pandas",
    "numpy": "NumPy", "springboot": "Spring Boot", "dotnet": ".NET", "aspnet": "ASP.NET",
    "angular": "Angular", "vue": "Vue", "svelte": "Svelte", "reactnative": "React Native",
    "jenkins": "Jenkins", "terraform": "Terraform", "ansible": "Ansible", "linux": "Linux",
    "kafka": "Kafka", "spark": "Spark", "hadoop": "Hadoop", "airflow": "Airflow",
    "snowflake": "Snowflake", "bigquery": "BigQuery", "elasticsearch": "Elasticsearch",
    "mysql": "MySQL", "dynamodb": "DynamoDB", "cassandra": "Cassandra", "graphql": "GraphQL",
    "grpc": "gRPC", "kotlin": "Kotlin", "scala": "Scala", "php": "PHP", "selenium": "Selenium",
    "playwright": "Playwright", "junit": "JUnit", "pytest": "pytest", "tableau": "Tableau",
    "powerbi": "Power BI", "langchain": "LangChain", "opencv": "OpenCV", "keras": "Keras",
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask", "nestjs": "NestJS",
    "microservices": "Microservices", "serverless": "Serverless", "githubactions": "GitHub Actions",
}  # fmt: skip
_SAME = {"llms": "llm", "gcp": "googlecloud", "k8s": "kubernetes", "golang": "go",
         "net": "dotnet", ".net": "dotnet", "aspnet": "aspnet"}  # fmt: skip
_TECH = {skill_key(t) for t in COMMON_TECH}
MIN_JOBS = 2  # a skill asked for by one job is noise, not a pattern


def _canon(key: str) -> str:
    return _SAME.get(key, key)


def job_skill_keys(job: Job, page_text: str, post_text: str) -> set[str]:
    """Every technology this job names: its stated skills, and known names in its text."""
    stated: list[str] = []
    if job.data_json:
        data: dict[str, Any] = json.loads(job.data_json)
        stated = technical([str(s) for s in (data.get("skills_required") or [])
                            + (data.get("skills_preferred") or [])])  # fmt: skip
    keys = {_canon(skill_key(s)) for s in stated}
    in_text = {_canon(k) for k in keys_in(f"{page_text}\n{post_text}") if k in _TECH}
    return (keys | {k for k in in_text if k not in AMBIGUOUS}) - {""}


@dataclass
class Gap:
    name: str
    jobs: int


def skill_gaps(
    repo: Repo, profile: dict[str, Any], since: datetime, min_score: int, top: int = 8
) -> tuple[list[Gap], int]:
    """(the skills your good matches since `since` named most that you don't have, how many
    good matches were looked at)."""
    have = {_canon(k) for k in profile_skill_keys(profile)}
    counts: Counter[str] = Counter()
    jobs = [
        j for j in repo.jobs_created_since(to_iso(since))
        if (j.score or 0) >= min_score and j.status != JobStatus.DISCARDED.value
    ]  # fmt: skip
    for job in jobs:
        page = repo.get_page(job.id)
        keys = job_skill_keys(job, (page.text or "") if page else "", own_post(repo, job.id) or "")
        counts.update(keys - have)
    named = [Gap(NAMES.get(k, k), n) for k, n in counts.items() if n >= MIN_JOBS]
    gaps = sorted(named, key=lambda g: (-g.jobs, g.name.casefold()))
    return gaps[:top], len(jobs)


def gap_line(gaps: list[Gap]) -> str:
    """'Docker in 14 good matches, AWS in 9, Kubernetes in 6'."""
    if not gaps:
        return ""
    first, *rest = gaps
    return ", ".join([f"{first.name} in {first.jobs} good matches"]
                     + [f"{g.name} in {g.jobs}" for g in rest])  # fmt: skip
