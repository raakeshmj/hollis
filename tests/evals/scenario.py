"""Deterministic multi-session agent trajectories with probes, for memory evaluation.

Each scenario is a sequence of sessions. A session is a list of statements: things a user said,
or observations an agent's tools produced (machine-generated log lines, as AMA-Bench stresses).
Planted statements carry the evidence probes need; seeded noise statements (other services,
owners, regions, versions, log lines) fill the history so it exceeds the context budget, and
share vocabulary with the planted ones so retrieval has to discriminate.

Probe categories follow LongMemEval (information extraction, multi-session reasoning, knowledge
update, temporal reasoning, abstention) and MemoryAgentBench (accurate retrieval, test-time
learning, long-range understanding, selective forgetting), plus temporary conditions, causal
evidence from agent logs (AMA-Bench) and cross-user isolation.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from hollis.memory import ChangeKind
from hollis.runtime.memory import MemoryKind

START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


class Source(StrEnum):
    USER = "user"
    TOOL = "tool"


@dataclass(frozen=True)
class Statement:
    sid: str
    session: int
    text: str
    owner: str = "alice"
    source: Source = Source.USER
    kind: MemoryKind = MemoryKind.FACT
    subject: str | None = None
    key: str | None = None
    change: ChangeKind = ChangeKind.NEW
    valid_days: float | None = None
    """For temporary statements: how long they hold."""
    forget: bool = False
    """The user later asks for this to be forgotten (in the session after it)."""

    @property
    def at(self) -> datetime:
        return session_time(self.session)


@dataclass(frozen=True)
class Probe:
    pid: str
    category: str
    question: str
    required: tuple[str, ...] = ()
    """Statement ids whose evidence must be in context."""
    forbidden: tuple[str, ...] = ()
    """Statement ids that must not be offered as current evidence (stale, wrong, deleted,
    another user's)."""
    session: int | None = None
    """Ask as of the end of this session (default: after all sessions)."""
    answer: tuple[str, ...] = ()
    """Keywords a correct live answer contains (used only by the opt-in live evaluation)."""


@dataclass
class Scenario:
    name: str
    sessions: int
    statements: list[Statement] = field(default_factory=list[Statement])
    probes: list[Probe] = field(default_factory=list[Probe])

    def by_id(self) -> dict[str, Statement]:
        return {s.sid: s for s in self.statements}


def session_time(session: int) -> datetime:
    return START + timedelta(days=session, hours=1)


def probe_time(scenario: Scenario, probe: Probe) -> datetime:
    session = probe.session if probe.session is not None else scenario.sessions
    return session_time(session) + timedelta(hours=8)


SERVICES = [
    "inventory",
    "search",
    "auth",
    "ledger",
    "notifications",
    "checkout",
    "catalog",
    "pricing",
    "shipping",
    "reporting",
    "identity",
    "gateway",
    "media",
    "recommendations",
    "fraud",
]
TEAMS = ["Heron", "Osprey", "Kestrel", "Merlin", "Harrier", "Condor", "Plover", "Swift"]
REGIONS = ["eu-west-1", "us-east-2", "ap-south-1", "eu-north-1", "us-west-2"]
ENGINES = ["MySQL 8", "Cassandra 4", "Redis 7", "DynamoDB", "MongoDB 7", "ClickHouse 24"]
DAYS = ["Tuesday", "Wednesday", "Thursday", "Friday"]


def _noise(rng: random.Random, session: int, index: int) -> Statement:
    service = rng.choice(SERVICES)
    template = rng.randrange(6)
    sid = f"n{session}-{index}"
    if template == 0:
        team = rng.choice(TEAMS)
        return Statement(
            sid,
            session,
            f"The {service} service is owned by the {team} team.",
            subject=f"{service} service",
            key="owner",
        )
    if template == 1:
        return Statement(
            sid,
            session,
            f"The {service} service is deployed in {rng.choice(REGIONS)}.",
            subject=f"{service} service",
            key="region",
        )
    if template == 2:
        return Statement(
            sid,
            session,
            f"The {service} database runs {rng.choice(ENGINES)}.",
            subject=f"{service} database",
            key="engine",
        )
    if template == 3:
        team = rng.choice(TEAMS)
        return Statement(
            sid,
            session,
            f"The {team} team's on-call handover is every {rng.choice(DAYS)}.",
            subject=f"{team.lower()} team",
            key="handover",
        )
    if template == 4:
        code = rng.choice([1, 2, 124, 139, 143])
        job = f"{service}-sync"
        line = (
            f"job={job} run={rng.randrange(1000, 9999)} status=failed exit_code={code} "
            f"host=worker-{rng.randrange(1, 20)} at={session_time(session):%Y-%m-%dT%H:%MZ}"
        )
        return Statement(
            sid, session, line, source=Source.TOOL, kind=MemoryKind.OBSERVATION, subject=job
        )
    return Statement(
        sid,
        session,
        f"Reminder: the {service} dashboard lives at "
        f"https://grafana.acme.example/d/{service}-{rng.randrange(100, 999)}.",
    )


def build(*, noise_per_session: int = 40, sessions: int = 12, seed: int = 7) -> Scenario:
    rng = random.Random(seed)  # noqa: S311  # reproducible test data, not security
    s = Scenario(name=f"ops-assistant-{noise_per_session}x{sessions}", sessions=sessions)
    planted = [
        Statement(
            "owner",
            1,
            "The payments service is owned by the Falcon team.",
            subject="payments service",
            key="owner",
        ),
        Statement(
            "workdays", 1, "Alice works Monday to Thursday.", subject="alice", key="availability"
        ),
        Statement(
            "handover",
            2,
            "The Falcon team's on-call handover happens every Monday at 09:00.",
            subject="falcon team",
            key="handover",
        ),
        Statement(
            "oom",
            2,
            "Exit code 137 means the process was OOM-killed; the fix is to raise "
            "the job's memory limit.",
            kind=MemoryKind.PROCEDURE,
            subject="exit code 137",
        ),
        Statement(
            "format",
            3,
            "Alice wants incident summaries as short bullet points.",
            kind=MemoryKind.PREFERENCE,
            subject="alice",
            key="summary format",
        ),
        Statement(
            "pg14",
            4,
            "The billing database runs PostgreSQL 14.",
            subject="billing database",
            key="engine",
        ),
        Statement(
            "vpn1",
            5,
            "The VPN gateway hostname is vpn-1.acme.internal.",
            subject="vpn gateway",
            key="hostname",
        ),
        Statement(
            "leave",
            6,
            "Alice is on leave for the next five days.",
            subject="alice",
            key="availability",
            change=ChangeKind.TEMPORARY,
            valid_days=5,
        ),
        Statement(
            "vpn2",
            7,
            "Correction: the VPN gateway hostname is vpn-2.acme.internal.",
            subject="vpn gateway",
            key="hostname",
            change=ChangeKind.CORRECTION,
        ),
        Statement(
            "etl",
            8,
            "job=etl-nightly run=4411 status=failed exit_code=137 host=worker-7 "
            f"at={session_time(8):%Y-%m-%dT%H:%MZ}",
            source=Source.TOOL,
            kind=MemoryKind.OBSERVATION,
            subject="etl-nightly",
        ),
        Statement(
            "pg16",
            9,
            "The billing database was upgraded to PostgreSQL 16.",
            subject="billing database",
            key="engine",
            change=ChangeKind.UPDATE,
        ),
        Statement(
            "phone",
            10,
            "Alice's personal phone number is 555-0199.",
            subject="alice",
            key="phone",
            forget=True,
        ),
        Statement(
            "bob-review",
            10,
            "Bob's payments service performance review is on March 30.",
            owner="bob",
            subject="bob",
            key="review",
        ),
    ]
    for session in range(sessions):
        for index in range(noise_per_session):
            s.statements.append(_noise(rng, session, index))
        s.statements.extend(p for p in planted if p.session == session)
    s.statements.sort(key=lambda st: (st.session, st.sid.startswith("n"), st.sid))
    rng.shuffle(s.statements)  # interleave planted and noise within each session...
    s.statements.sort(key=lambda st: st.session)  # ...while keeping sessions in order
    s.probes = [
        Probe(
            "ie",
            "information_extraction",
            "Who owns the payments service?",
            required=("owner",),
            answer=("falcon",),
        ),
        Probe(
            "multi",
            "multi_session",
            "When does the team that owns the payments service hand over on-call?",
            required=("owner", "handover"),
            answer=("monday",),
        ),
        Probe(
            "pref",
            "preference",
            "How should I format the incident summary for Alice?",
            required=("format",),
            answer=("bullet",),
        ),
        Probe(
            "update",
            "knowledge_update",
            "Which PostgreSQL version does the billing database run?",
            required=("pg16",),
            forbidden=("pg14",),
            answer=("16",),
        ),
        Probe(
            "temporal",
            "temporal",
            "Which PostgreSQL version does the billing database run?",
            required=("pg14",),
            forbidden=("pg16",),
            session=6,
            answer=("14",),
        ),
        Probe(
            "correction",
            "knowledge_update",
            "What is the VPN gateway hostname?",
            required=("vpn2",),
            forbidden=("vpn1",),
            answer=("vpn-2",),
        ),
        Probe(
            "temp-during",
            "temporary",
            "Is Alice available to review this?",
            required=("leave",),
            session=7,
            answer=("leave",),
        ),
        Probe(
            "temp-after",
            "temporary",
            "Is Alice available to review this?",
            required=("workdays",),
            forbidden=("leave",),
            answer=("monday",),
        ),
        Probe(
            "causal",
            "agent_logs",
            "Why did the nightly ETL job fail, and how do we fix it?",
            required=("etl", "oom"),
            answer=("memory",),
        ),
        Probe(
            "forget",
            "forgetting",
            "What is Alice's personal phone number?",
            forbidden=("phone",),
            answer=(),
        ),
        Probe(
            "isolation",
            "isolation",
            "When is the payments service performance review?",
            forbidden=("bob-review",),
            answer=(),
        ),
        Probe(
            "abstain",
            "abstention",
            "Which message broker does the payments service use?",
            answer=(),
        ),
    ]
    return s


TOPICS = [
    "graph neural networks",
    "speech recognition",
    "protein folding",
    "query optimization",
    "federated learning",
    "image segmentation",
    "program synthesis",
    "time series anomaly",
]
VENUES = ["NeurIPS", "ICML", "ACL", "VLDB", "CVPR", "KDD"]
PEOPLE = ["Priya", "Tomas", "Mei", "Jonas", "Amara", "Luis"]


def _research_noise(rng: random.Random, session: int, index: int) -> Statement:
    sid = f"n{session}-{index}"
    topic = rng.choice(TOPICS)
    match rng.randrange(5):
        case 0:
            return Statement(
                sid,
                session,
                f"The {topic} survey draft is due at {rng.choice(VENUES)}.",
                subject=f"{topic} survey",
                key="venue",
            )
        case 1:
            person = rng.choice(PEOPLE)
            return Statement(
                sid,
                session,
                f"{person} is the lead author on the {topic} paper.",
                subject=f"{topic} paper",
                key="lead",
            )
        case 2:
            return Statement(
                sid,
                session,
                f"The {topic} benchmark uses {rng.randrange(2, 40)}k "
                f"examples split {rng.choice(['80/10/10', '70/15/15', '90/5/5'])}.",
                subject=f"{topic} benchmark",
                key="size",
            )
        case 3:
            line = (
                f"eval run={rng.randrange(100, 999)} model={topic.split()[0]}-base "
                f"metric=f1 value=0.{rng.randrange(40, 95)} gpu=a100 hours={rng.randrange(1, 30)}"
            )
            return Statement(
                sid,
                session,
                line,
                source=Source.TOOL,
                kind=MemoryKind.OBSERVATION,
                subject=f"{topic.split()[0]}-base",
            )
        case _:
            return Statement(
                sid,
                session,
                f"Reading list: {rng.choice(PEOPLE)} recommended a "
                f"{rng.choice(VENUES)} paper on {topic}.",
            )


def build_research(*, noise_per_session: int = 40, sessions: int = 10, seed: int = 11) -> Scenario:
    """A held-out scenario, written after retrieval tuning and not tuned against: a research
    assistant, different vocabulary, and questions paraphrased away from the stored wording."""
    rng = random.Random(seed)  # noqa: S311  # reproducible test data, not security
    s = Scenario(name=f"research-assistant-{noise_per_session}x{sessions}", sessions=sessions)
    planted = [
        Statement(
            "cite",
            0,
            "Sofia wants every claim in the literature review backed by a citation with a DOI.",
            kind=MemoryKind.PREFERENCE,
            subject="sofia",
            key="citations",
        ),
        Statement(
            "lead",
            1,
            "Rafael is the lead author on the contrastive pretraining paper.",
            subject="contrastive pretraining paper",
            key="lead",
        ),
        Statement(
            "rafael-tz",
            2,
            "Rafael works from Lisbon and is unavailable after 18:00 Lisbon time.",
            subject="rafael",
            key="hours",
        ),
        Statement(
            "ds-v1",
            2,
            "The contrastive pretraining corpus is CC-News-2024.",
            subject="contrastive pretraining corpus",
            key="dataset",
        ),
        Statement(
            "gpu-fail",
            4,
            "eval run=777 model=contrastive-large status=failed "
            "error=CUDA_ERROR_OUT_OF_MEMORY batch=512 gpu=a100",
            source=Source.TOOL,
            kind=MemoryKind.OBSERVATION,
            subject="contrastive-large",
        ),
        Statement(
            "gpu-fix",
            3,
            "When a run fails with CUDA out of memory, halve the batch size and "
            "enable gradient checkpointing.",
            kind=MemoryKind.PROCEDURE,
            subject="cuda out of memory",
        ),
        Statement(
            "ds-v2",
            6,
            "The contrastive pretraining corpus was switched to CC-News-2025.",
            subject="contrastive pretraining corpus",
            key="dataset",
            change=ChangeKind.UPDATE,
        ),
        Statement(
            "deadline-temp",
            7,
            "The camera-ready deadline is extended by three days.",
            subject="camera-ready",
            key="deadline",
            change=ChangeKind.TEMPORARY,
            valid_days=3,
        ),
        Statement(
            "deadline",
            1,
            "The camera-ready deadline is the 15th.",
            subject="camera-ready",
            key="deadline",
        ),
        Statement(
            "address",
            5,
            "Sofia's home address is 4 Rua das Flores.",
            subject="sofia",
            key="address",
            forget=True,
        ),
        Statement(
            "other",
            5,
            "Kenji's contrastive pretraining grant was rejected.",
            owner="bob",
            subject="kenji",
            key="grant",
        ),
    ]
    for session in range(sessions):
        for index in range(noise_per_session):
            s.statements.append(_research_noise(rng, session, index))
        s.statements.extend(p for p in planted if p.session == session)
    rng.shuffle(s.statements)
    s.statements.sort(key=lambda st: st.session)
    s.probes = [
        Probe(
            "ie",
            "information_extraction",
            "Who is leading the contrastive pretraining work?",
            required=("lead",),
            answer=("rafael",),
        ),
        Probe(
            "multi",
            "multi_session",
            "Until what time can I reach the lead of the contrastive pretraining paper?",
            required=("lead", "rafael-tz"),
            answer=("18",),
        ),
        Probe(
            "pref",
            "preference",
            "How should references be handled in Sofia's literature review?",
            required=("cite",),
            answer=("doi",),
        ),
        Probe(
            "update",
            "knowledge_update",
            "Which corpus are we pretraining the contrastive model on?",
            required=("ds-v2",),
            forbidden=("ds-v1",),
            answer=("2025",),
        ),
        Probe(
            "temporal",
            "temporal",
            "Which corpus are we pretraining the contrastive model on?",
            required=("ds-v1",),
            forbidden=("ds-v2",),
            session=4,
            answer=("2024",),
        ),
        Probe(
            "temp-during",
            "temporary",
            "When is the camera-ready due?",
            required=("deadline-temp",),
            session=8,
            answer=("extended",),
        ),
        Probe(
            "temp-after",
            "temporary",
            "When is the camera-ready due?",
            required=("deadline",),
            forbidden=("deadline-temp",),
            answer=("15",),
        ),
        Probe(
            "causal",
            "agent_logs",
            "The contrastive-large evaluation crashed. What happened and what should I change?",
            required=("gpu-fail", "gpu-fix"),
            answer=("batch",),
        ),
        Probe("forget", "forgetting", "Where does Sofia live?", forbidden=("address",)),
        Probe(
            "isolation",
            "isolation",
            "What happened to the contrastive pretraining grant?",
            forbidden=("other",),
        ),
        Probe("abstain", "abstention", "Which tokenizer does the contrastive model use?"),
    ]
    return s
