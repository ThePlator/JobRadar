"""What a skill you're missing really means, job by job (the Skills page's "What exactly?").

"C++ in 26 jobs" hides two different things: "Strong C++ on Linux" and "Python, Java or C++".
The second one you already meet with Python. So each job's sentences naming the skill are
read and sorted into:

- must: asked for outright ("Strong systems programming in C and C++");
- either: one of a few alternatives you don't have yet ("AWS, Azure or GCP");
- nice: a plus ("Exposure to AWS is a plus", "cloud basics");
- covered: one of a few alternatives and you have another ("Python, Java or C++");
- listed: only in the job's stated skill list, with nothing in its text to go on.

And what exactly they name with it: AWS's services (Lambda, S3), C++'s topics (STL,
multithreading), and the other technologies asked for in the same breath. Rules over stored
text, like the counts themselves: no LLM call, so the page opens instantly.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from jobradar.pipeline.skill_gaps import _TECH, NAMES, _canon
from jobradar.resume.validate import keys_in

Kind = Literal["must", "either", "nice", "covered", "listed"]
ORDER: dict[str, int] = {"must": 0, "listed": 1, "either": 2, "nice": 3, "covered": 4}

# Technologies that stand in for one another: "Python, Java or C++" asks for one of them.
FAMILIES = [
    {"python", "java", "c++", "c#", "javascript", "typescript", "go", "rust", "kotlin", "scala",
     "ruby", "php", "perl", "swift", "dart"},
    {"aws", "azure", "googlecloud", "ibmcloud", "oraclecloud"},
    {"react", "angular", "vue", "svelte", "nextjs"},
    {"mysql", "postgresql", "sqlite", "mongodb", "dynamodb", "cassandra", "redis", "oracle"},
    {"tensorflow", "pytorch", "keras", "scikitlearn", "jax"},
    {"tableau", "powerbi", "looker", "thoughtspot"},
    {"selenium", "playwright", "cypress"},
    {"jenkins", "githubactions", "gitlab", "circleci"},
    {"django", "flask", "fastapi", "springboot", "express", "nestjs", "rails", "laravel"},
]  # fmt: skip
_ALTERNATIVE = re.compile(
    r"\b(or|either|any(?: one)?|one of|one or more|two or more|at least one|such as|e\.g\.|"
    r"similar|equivalent|like)\b", re.I,
)  # fmt: skip
_NICE = re.compile(
    r"\b(plus|preferred|preferably|good to have|nice to have|desirable|advantage|bonus|"
    r"familiar(?:ity)?|exposure|aware(?:ness)?|basics?|interest in|optional|would help|"
    r"not required|none of (?:these|them) (?:are|is) required|added benefit|certif\w*)\b", re.I,
)  # fmt: skip
# Sentences about the company, not the job: "is an AWS partner", "customers include AWS".
_NOT_ASKED = re.compile(
    r"\b(partner|customers?|clients?|backed by|enterprises|used by|trusted by|investors?)\b", re.I
)
# Prep advice that some job pages add ("Build one AWS serverless project …"): not the job's ask.
_ADVICE = re.compile(r"^(revise|build (?:or revise )?one|practi[sc]e|drill|prepare|brush up|"
                     r"learn|read up)\b", re.I)  # fmt: skip
_URL = re.compile(r"https?://\S+")
_SPLIT = re.compile(r"\n+|(?<=[.!?;])\s+(?=[A-Z0-9])|\s[-\u2013\u2022\u00b7]\s|\s\d\.\s")
_ASKS = re.compile(
    r"\b(experience|knowledge|understanding|proficien\w+|strong|hands-on|skills?|develop|"
    r"build|work(?:ing)? with|required|must)\b", re.I,
)  # fmt: skip
SHORT = 3  # words: a fragment this short is a list item ("- C/C++"), joined to the line before
CONTEXT = 2  # sentences after a mention that its topics may come from ("- Multi-threaded …")

# What each skill is made of, as the jobs name it: (label, pattern).
TOPICS: dict[str, list[tuple[str, str]]] = {
    "aws": [
        ("Lambda", r"\blambda\b"), ("S3", r"\bS3\b"), ("EC2", r"\bEC2\b"),
        ("API Gateway", r"\bapi gateway\b"), ("DynamoDB", r"\bdynamo ?db\b"),
        ("RDS / Aurora", r"\b(RDS|aurora)\b"), ("IAM", r"\bIAM\b"), ("VPC", r"\bVPC\b"),
        ("ECS / Fargate", r"\b(ECS|fargate)\b"), ("EKS", r"\bEKS\b"),
        ("CloudFormation / CDK", r"\b(cloudformation|CDK)\b"), ("CloudWatch", r"\bcloudwatch\b"),
        ("CloudFront", r"\bcloudfront\b"), ("Cognito", r"\bcognito\b"),
        ("SQS / SNS", r"\b(SQS|SNS)\b"), ("Kinesis", r"\bkinesis\b"),
        ("SageMaker", r"\bsagemaker\b"), ("Bedrock", r"\bbedrock\b"), ("Glue", r"\bglue\b"),
        ("Redshift", r"\bredshift\b"), ("AMIs", r"\bAMIs?\b"),
        ("Serverless", r"\bserverless\b"), ("Free tier deploys", r"\bfree tier\b"),
        ("Certification", r"\b(certifi\w+|developer associate|cloud practitioner|"
                          r"solutions architect)\b"),
    ],
    "azure": [
        ("Azure Functions", r"\b(azure )?functions\b"), ("App Service", r"\bapp services?\b"),
        ("AKS", r"\bAKS\b"), ("Azure DevOps", r"\bazure devops\b"),
        ("Blob Storage", r"\bblob\b"), ("Cosmos DB", r"\bcosmos\b"),
        ("Azure ML", r"\b(azure ml|machine learning studio)\b"),
        ("Azure OpenAI", r"\bazure openai\b"), ("Data Factory", r"\bdata factory\b"),
        ("Certification", r"\b(certifi\w+|AZ-\d+|developer associate|fundamentals)\b"),
    ],
    "googlecloud": [
        ("BigQuery", r"\bbigquery\b"), ("Cloud Run", r"\bcloud run\b"), ("GKE", r"\bGKE\b"),
        ("Vertex AI", r"\bvertex\b"), ("Cloud Functions", r"\bcloud functions\b"),
        ("Pub/Sub", r"\bpub ?/ ?sub\b"), ("Cloud Storage", r"\bcloud storage\b"),
        ("Certification", r"\bcertifi\w+\b"),
    ],
    "c++": [
        ("Modern C++ (11/14/17/20)", r"\bC\+\+ ?(11|14|17|20|23)\b|\bmodern C\+\+"),
        ("STL", r"\bSTL\b|standard template"), ("Templates", r"\btemplates?\b"),
        ("OOP / design", r"\b(object[- ]oriented|OOP|OOPS|design patterns?)\b"),
        ("Memory and pointers", r"\b(memory management|pointers?|smart pointers?|RAII)\b"),
        ("Multithreading", r"\b(multi[- ]?thread\w*|concurren\w+|threads?)\b"),
        ("Data structures and algorithms", r"\b(data structures?|algorithms?|DSA)\b"),
        ("Linux / Unix systems", r"\b(linux|unix|posix|operating systems?|OS internals)\b"),
        ("IPC and shared memory", r"\b(IPC|shared memory|signals)\b"),
        ("Networking", r"\b(networking|sockets?|TCP|protocols?|layer ?[23])\b"),
        ("Embedded / drivers", r"\b(embedded|device drivers?|firmware|kernel)\b"),
        ("Performance", r"\b(performance|optimi[sz]\w+|low[- ]latency)\b"),
        ("Games / 3D", r"\b(games?|3D|graphics|unreal)\b"),
        ("Debugging (gdb)", r"\b(gdb|debugg\w+|valgrind)\b"),
    ],
    "linux": [
        ("Shell / Bash scripting", r"\b(shell|bash|scripting)\b"),
        ("Command line", r"\bcommand[- ]line\b|\bCLI\b"),
        ("Internals / kernel", r"\b(internals|kernel|signals|IPC|file systems?)\b"),
        ("System administration", r"\b(admin\w*|sysadmin|RHEL|red hat|ubuntu|centos)\b"),
        ("Networking", r"\b(networking|TCP|DNS|nginx)\b"),
        ("Containers", r"\b(containers?|docker|podman)\b"),
    ],
    "kubernetes": [
        ("Helm", r"\bhelm\b"), ("OpenShift", r"\bopenshift\b"),
        ("Deployments / pods", r"\b(deployments?|pods?|services)\b"),
        ("Operators", r"\boperators?\b"), ("EKS / AKS / GKE", r"\b(EKS|AKS|GKE)\b"),
        ("Certification (CKA/CKAD)", r"\b(CKA|CKAD|certifi\w+)\b"),
    ],
    "docker": [
        ("Docker Compose", r"\bcompose\b"), ("Images / Dockerfiles", r"\b(images?|dockerfiles?)\b"),
        ("Containers in CI/CD", r"\bCI ?/ ?CD\b"), ("Kubernetes", r"\b(kubernetes|k8s)\b"),
    ],
    "generativeai": [
        ("LLMs", r"\bLLMs?\b|large language"), ("RAG", r"\bRAG\b|retrieval"),
        ("Prompt engineering", r"\bprompt\w*"), ("Agents", r"\bagent\w*"),
        ("LangChain / LangGraph", r"\blang(chain|graph)\b"), ("Fine-tuning", r"\bfine[- ]?tun\w+"),
        ("Embeddings / vector DBs", r"\b(embeddings?|vector)\b"),
        ("OpenAI / Gemini APIs", r"\b(openai|gpt|gemini|claude)\b"),
    ],
}  # fmt: skip
# How the skill itself is written, for highlighting it in a quote.
_SPELLINGS: dict[str, str] = {
    "aws": r"AWS|Amazon Web Services", "googlecloud": r"Google Cloud(?: Platform)?|GCP",
    "c++": r"C\s*/\s*C\+\+|C\+\+", "c#": r"C#", "kubernetes": r"Kubernetes|K8s",
    "generativeai": r"Gen(?:erative)?\s?AI", "dotnet": r"\.NET", "llm": r"LLMs?",
}  # fmt: skip


@dataclass
class Mention:
    text: str
    kind: Kind
    alternatives: list[str]  # the other choices in an "A, B or C"


@dataclass
class JobNeed:
    job_id: str
    kind: Kind
    quote: Mention | None  # the sentence that shows it best
    alternatives: list[str]


@dataclass
class Topic:
    label: str
    jobs: list[str]  # job ids


@dataclass
class Detail:
    key: str
    needs: list[JobNeed]
    topics: list[Topic]  # what exactly they name, most asked first
    alongside: list[tuple[str, int]]  # (skill key, jobs): asked for in the same breath
    counts: Counter[str] = field(default_factory=Counter)  # kind -> jobs

    @property
    def needed(self) -> int:
        """Jobs you can't meet without it: everything but "covered"."""
        return len(self.needs) - self.counts["covered"]

    @property
    def covered_by(self) -> list[str]:
        """Your skills that stood in for it, most used first."""
        c = Counter(a for n in self.needs if n.kind == "covered" for a in n.alternatives)
        return [k for k, _ in c.most_common()]


def name(key: str) -> str:
    return NAMES.get(key, key[:1].upper() + key[1:])


@lru_cache(maxsize=1024)
def sentences(text: str) -> tuple[str, ...]:
    """The text's sentences and bullets; a bulleted list of single items ("languages
    including - Java - C/C++ - Python") stays one sentence, so its "or" still reads."""
    out: list[str] = []
    joined = False  # the last sentence already has list items joined to it
    for part in _SPLIT.split(_URL.sub("", text)):
        if not (p := " ".join(part.split()).strip(" -\u2013\u2022,")):
            continue
        short = len(p.split()) <= SHORT and not p.endswith(":")  # "Skills:" heads a list
        if out and short and not out[-1].endswith((".", "!", "?")):
            sep = " " if out[-1].endswith(":") else ", " if joined else ": "
            out[-1] = f"{out[-1]}{sep}{p}"
            joined = True
        else:
            out.append(p)
            joined = False
    return tuple(out)


@lru_cache(maxsize=50_000)
def _keys(sentence: str) -> frozenset[str]:
    return frozenset({_canon(k) for k in keys_in(sentence) if k in _TECH or _canon(k) in _TECH})


def mention(sentence: str, key: str, have: set[str]) -> Mention | None:
    """How a sentence naming the skill asks for it; None when it isn't asking."""
    if _NOT_ASKED.search(sentence) or _ADVICE.search(sentence):
        return None
    family = next((f for f in FAMILIES if key in f), set())
    others = sorted((_keys(sentence) & family) - {key})
    alternatives = others if others and _ALTERNATIVE.search(sentence) else []
    if alternatives and set(alternatives) & have:
        return Mention(sentence, "covered", [a for a in alternatives if a in have])
    if _NICE.search(sentence):
        return Mention(sentence, "nice", alternatives)
    return Mention(sentence, "either" if alternatives else "must", alternatives)


def need(job_id: str, text: str, key: str, have: set[str]) -> tuple[JobNeed, list[str]]:
    """(what this job wants of the skill, the sentences around its mentions)."""
    parts = sentences(text)
    found: list[Mention] = []
    near: list[str] = []
    for i, sentence in enumerate(parts):
        if key in _keys(sentence) and (m := mention(sentence[:400], key, have)):
            found.append(m)
            near.extend(parts[i : i + 1 + CONTEXT])
    if not found:
        return JobNeed(job_id, "listed", None, []), near
    # A job is as strict as its strictest mention, but "covered" only if every one is.
    open_ = [m for m in found if m.kind != "covered"]
    best = min(open_ or found, key=lambda m: (ORDER[m.kind], not _ASKS.search(m.text),
                                              abs(len(m.text) - 140)))  # fmt: skip
    alts = sorted({a for m in (open_ or found) for a in m.alternatives})
    return JobNeed(job_id, best.kind, best, alts), near


def detail(key: str, job_ids: list[str], texts: dict[str, str], have: set[str]) -> Detail:
    needs: list[JobNeed] = []
    topic_jobs: dict[str, list[str]] = {}
    alongside: Counter[str] = Counter()
    patterns = [(label, re.compile(p, re.I)) for label, p in TOPICS.get(key, [])]
    for job_id in job_ids:
        n, near = need(job_id, texts.get(job_id, ""), key, have)
        needs.append(n)
        context = " \n".join(dict.fromkeys(near))
        for label, pattern in patterns:
            if pattern.search(context):
                topic_jobs.setdefault(label, []).append(job_id)
        if n.kind != "covered":
            asked = {k for s in near[:: CONTEXT + 1] for k in _keys(s)}
            alongside.update(asked - {key} - set(n.alternatives))
    rank = {label: i for i, (label, _) in enumerate(TOPICS.get(key, []))}
    topics = sorted((Topic(label, ids) for label, ids in topic_jobs.items()),
                    key=lambda t: (-len(t.jobs), rank[t.label]))  # fmt: skip
    pairs = [(k, c) for k, c in alongside.most_common(10) if c >= 2]
    return Detail(key, needs, topics, pairs, Counter(n.kind for n in needs))


def spelling(key: str) -> re.Pattern[str]:
    """How the skill shows up in a quote, for highlighting it."""
    if key in _SPELLINGS:
        return re.compile(rf"(?<![\w+#])({_SPELLINGS[key]})(?![\w+#])", re.I)
    return re.compile(rf"(?<![\w+#])({re.escape(name(key))})(?![\w+#])", re.I)
