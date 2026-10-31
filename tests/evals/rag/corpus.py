"""Deterministic retrieval-evaluation corpora with graded relevance labels.

Relevance is defined by *facts*, not chunk ids, so labels survive chunking changes: a chunk is
relevant to a query if it contains one of the query's fact phrases (grade 2 for primary facts,
1 for supporting facts). Queries with no facts are unanswerable from the corpus.

`engineering()` is the development corpus (configuration choices were made on it).
`clinical_ops()` is held out: written after the defaults were chosen, never tuned against.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tests.knowledge.fixtures import docx


@dataclass(frozen=True)
class Doc:
    key: str
    data: bytes
    effective: datetime | None = None
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Query:
    qid: str
    category: str
    text: str
    primary: tuple[str, ...] = ()
    """Fact phrases that answer the query (grade 2)."""
    supporting: tuple[str, ...] = ()
    """Facts that help (grade 1); for multi-hop, the bridge fact."""
    required: tuple[str, ...] = ()
    """Facts that must all be retrieved for full evidence coverage (default: primary)."""
    answer: tuple[str, ...] = ()
    """Keywords of a correct answer (live evaluation only)."""

    @property
    def facts(self) -> tuple[str, ...]:
        return self.required or self.primary


@dataclass
class Corpus:
    name: str
    docs: list[Doc] = field(default_factory=list[Doc])
    queries: list[Query] = field(default_factory=list[Query])


def _noise_runbooks(rng: random.Random, count: int) -> list[Doc]:
    services = ["inventory", "search", "auth", "catalog", "pricing", "shipping", "media", "fraud",
                "identity", "notifications", "reporting", "gateway", "recommendations", "billing-ui"]  # fmt: skip
    actions = ["restart the pods", "scale the deployment", "flush the cache", "rotate the credentials",
               "drain the queue", "roll back the release"]  # fmt: skip
    docs: list[Doc] = []
    for i in range(count):
        service = services[i % len(services)]
        action = rng.choice(actions)
        code = f"E{rng.randrange(1000, 9999)}"
        text = (
            f"# {service.title()} service runbook {i}\n\n"
            f"## Restart procedure\n\nTo recover the {service} service, {action} and watch the error "
            f"rate dashboard for ten minutes. Error {code} indicates a transient upstream timeout.\n\n"
            f"## Ownership\n\nThe {service} service is owned by team {rng.choice(['Heron', 'Osprey', 'Kestrel', 'Merlin'])}. "
            f"On-call rotates every {rng.choice(['Tuesday', 'Wednesday', 'Thursday'])}.\n"
        )
        docs.append(Doc(f"runbooks/{service}-{i}.md", text.encode()))
    return docs


def engineering(*, noise: int = 30, seed: int = 5) -> Corpus:
    rng = random.Random(seed)  # noqa: S311  # reproducible test data
    corpus = Corpus(name=f"engineering-{noise}")
    corpus.docs = [
        Doc("runbooks/gpu-training.md", b"""# GPU training runbook

## Out of memory

When a training job fails because CUDA ran out of memory, halve the batch size and enable
gradient checkpointing before retrying.

## Driver faults

Error NVRM_XID_79 means the GPU has fallen off the bus. Drain the node and open a hardware
ticket; do not retry jobs on that node.
"""),
        Doc("logs/train-4411.txt", b"""2026-03-09T02:14:07Z job=etl-nightly run=4411 host=worker-7
2026-03-09T02:14:09Z status=failed exit_code=137 error=CUDA_ERROR_OUT_OF_MEMORY batch=512
"""),
        Doc("api/jobs.html", b"""<!doctype html><html><head><title>Jobs API</title></head><body>
<h1>Jobs API</h1><h2>Errors</h2>
<p>Error E4021 is returned when the job quota for the project is exhausted. Request a quota
increase through the platform portal.</p>
<h2>Pagination</h2><p>List endpoints return at most 100 items; pass the <code>page_token</code>
from the previous response to continue.</p></body></html>"""),
        Doc("code/retry.py", b'''"""Retry helpers for platform clients."""

MAX_RETRIES = 5


def backoff_delay(attempt: int) -> float:
    """Exponential backoff: 0.5 seconds doubled per attempt, capped at 30 seconds."""
    return min(0.5 * 2 ** attempt, 30.0)


class RetryBudgetExceeded(Exception):
    """Raised when a client exhausts MAX_RETRIES attempts."""
'''),
        Doc("config/limits.json", b'{"storage": {"log_retention_days": 30, "backup_retention_days": 365}, "api": {"rate_limit_rps": 200}}'),
        Doc("data/error-codes.csv", b"code,meaning,action\nE7001,certificate expired,renew via cert-manager\nE7002,DNS resolution failed,check resolver config\n"),
        Doc("policies/travel.docx", docx(
            ("Title", "Travel and expenses policy"),
            ("Heading1", "Ground transportation"),
            ("", "Ground transportation such as cabs and ride-hailing is reimbursed when public transit is not practical, up to 80 EUR per trip."),
            ("Heading1", "Flights"),
            ("", "Economy class is required for flights shorter than six hours."),
        )),
        Doc("platform/ownership.md", b"""# Data ownership

The ledger database is owned by the payments service. Schema changes require a review by the
payments team.
"""),
        Doc("platform/oncall.md", b"""# On-call roster

The payments service on-call is staffed by team Falcon. Escalate to the Falcon lead after
fifteen minutes without acknowledgement.
"""),
        Doc("releases/changelog.txt", b"""Release 3.14.2 fixed the scheduler deadlock that stalled cron jobs after leap seconds.

Release 3.15.0 introduced regional failover for the metadata service.
"""),
        Doc("policies/freeze-2025.md", b"# Deployment freeze 2025\n\nThe deployment freeze runs from December 15 to January 5.\n",
            effective=datetime(2025, 1, 1, tzinfo=UTC)),
        Doc("policies/freeze-2026.md", b"# Deployment freeze 2026\n\nThe deployment freeze runs from December 20 to January 3.\n",
            effective=datetime(2026, 1, 1, tzinfo=UTC)),
        Doc("vendor/storage-faq.md", b"# Vendor storage FAQ\n\nThe vendor states that object storage is replicated across three zones.\n"),
        Doc("platform/storage.md", b"# Storage architecture\n\nOur object storage is replicated across two zones only.\n"),
    ]  # fmt: skip
    corpus.docs += _noise_runbooks(rng, noise)
    corpus.docs += [  # hard negatives: share surface words with paraphrase and synonym queries
        Doc("guides/graphics-drivers.md", b"# Graphics card drivers\n\nInstall the graphics card driver from the vendor portal before training new hires on CAD tools.\n"),
        Doc("guides/java-memory.md", b"# Java memory leaks\n\nWhen the heap has no memory left, capture a heap dump and restart the JVM.\n"),
        Doc("hr/training.md", b"# New hire training\n\nModel training sessions for new hires run every Monday; bring your laptop.\n"),
        Doc("facilities/airport.md", b"# Airport shuttle\n\nThe office shuttle to the airport leaves hourly from the main entrance; it is free for staff.\n"),
        Doc("guides/http-clients.md", b"# HTTP clients\n\nClients should wait for the server response before sending the next request.\n"),
    ]  # fmt: skip
    corpus.queries = [
        Query("exact-xid", "exact_identifier", "What does NVRM_XID_79 mean?", primary=("NVRM_XID_79 means the GPU has fallen off the bus",), answer=("bus",)),
        Query("error-code", "error_code", "error E4021", primary=("E4021 is returned when the job quota",), answer=("quota",)),
        Query("csv-code", "error_code", "What should I do about E7001?", primary=("| E7001 | certificate expired | renew via cert-manager |",), answer=("cert-manager",)),
        Query("version", "version", "What did release 3.14.2 fix?", primary=("3.14.2 fixed the scheduler deadlock",), answer=("deadlock",)),
        Query("symbol", "exact_identifier", "How many attempts does MAX_RETRIES allow?", primary=("MAX_RETRIES = 5",), answer=("5",)),
        Query("paraphrase", "paraphrase", "my model training crashed because the graphics card had no memory left, what now?",
              primary=("halve the batch size",), answer=("batch",)),
        Query("synonym", "synonym", "Can I expense a taxi to the airport?", primary=("Ground transportation such as cabs",), answer=("80",)),
        Query("synonym-zero-overlap", "synonym", "Is a rideshare refunded?", primary=("Ground transportation such as cabs",), answer=("80",)),
        Query("cuda-regression", "identifier_to_concept", "The ETL job failed with CUDA_ERROR_OUT_OF_MEMORY. How do I fix it?",
              primary=("halve the batch size",), supporting=("error=CUDA_ERROR_OUT_OF_MEMORY",), answer=("batch",)),
        Query("multi-hop", "multi_hop", "Which team is on call for the service that owns the ledger database?",
              primary=("payments service on-call is staffed by team Falcon",), supporting=("ledger database is owned by the payments service",),
              required=("payments service on-call is staffed by team Falcon", "ledger database is owned by the payments service"), answer=("falcon",)),
        Query("cross-doc", "cross_document", "Compare the retention periods for logs and backups.",
              primary=('storage.log_retention_days: 30', 'storage.backup_retention_days: 365'), answer=("30", "365")),
        Query("temporal", "temporal", "When is the deployment freeze this year?",
              primary=("December 20 to January 3",), supporting=("December 15 to January 5",), answer=("20",)),
        Query("conflict", "conflicting", "How many zones is object storage replicated across?",
              primary=("replicated across two zones only", "replicated across three zones"),
              required=("replicated across two zones only", "replicated across three zones"), answer=("two",)),
        Query("backoff", "paraphrase", "How long do clients wait between retries?", primary=("0.5 seconds doubled per attempt",), answer=("0.5",)),
        Query("distractor", "distractor", "What is the restart procedure for the pricing service?",
              primary=("To recover the pricing service",), answer=("pricing",)),
        Query("unanswerable", "unanswerable", "What is the parental leave policy for contractors?", answer=()),
        Query("unanswerable-2", "unanswerable", "Which message broker does the ledger use for events?", answer=()),
    ]  # fmt: skip
    return corpus


def clinical_ops(*, noise: int = 30, seed: int = 9) -> Corpus:
    """Held-out: a clinical laboratory operations corpus written after tuning."""
    rng = random.Random(seed)  # noqa: S311  # reproducible test data
    corpus = Corpus(name=f"clinical-ops-{noise}")
    instruments = ["centrifuge", "analyzer", "incubator", "freezer", "pipetting robot", "sequencer"]
    noise_docs: list[Doc] = []
    for i in range(noise):
        instrument = instruments[i % len(instruments)]
        noise_docs.append(
            Doc(
                f"sop/{instrument.replace(' ', '-')}-{i}.md",
                (
                    f"# {instrument.title()} SOP {i}\n\n## Daily check\n\nVerify the {instrument} temperature log and "
                    f"record deviations. Alarm A{rng.randrange(100, 999)} indicates a sensor warm-up delay.\n\n"
                    f"## Contacts\n\nThe {instrument} is maintained by the {rng.choice(['core', 'facilities', 'vendor'])} team.\n"
                ).encode(),
            )
        )
    corpus.docs = [
        Doc("sop/sample-storage.md", b"""# Sample storage SOP

## Biobank freezers

Plasma aliquots are stored at minus eighty degrees Celsius. Thawed aliquots must never be
refrozen; discard them and log the discard reason.
"""),
        Doc("alarms/codes.csv", b"code,meaning,action\nLIS-503,interface queue stalled,restart the HL7 listener\nFRZ-911,freezer door ajar,close door and check probe\n"),
        Doc("logs/seq-run-88.txt", b"2026-04-02T07:10Z instrument=sequencer run=88 status=aborted reason=FLOWCELL_PRESSURE_LOW\n"),
        Doc("sop/sequencer.md", b"""# Sequencer troubleshooting

## Flow cell pressure

If the flow cell pressure is low, reseat the flow cell and purge the fluidics line before
restarting the run.
"""),
        Doc("policies/specimen-transport.docx", docx(
            ("Title", "Specimen transport policy"),
            ("Heading1", "Couriers"),
            ("", "Specimens are moved between sites by the contracted courier in insulated boxes with temperature loggers."),
        )),
        Doc("org/assay-ownership.md", b"# Assay ownership\n\nThe troponin assay is owned by the cardiology core lab.\n"),
        Doc("org/core-contacts.md", b"# Core lab contacts\n\nThe cardiology core lab is led by Dr. Imani Okafor.\n"),
        Doc("releases/lis-notes.txt", b"LIS version 8.2.1 corrected the duplicate accession numbering bug.\n"),
        Doc("policies/retention.json", b'{"records": {"raw_data_retention_years": 10, "consent_form_retention_years": 25}}'),
    ]  # fmt: skip
    corpus.docs += noise_docs
    corpus.queries = [
        Query("code", "error_code", "What does alarm LIS-503 mean?", primary=("| LIS-503 | interface queue stalled |",), answer=("hl7",)),
        Query("version", "version", "What was fixed in LIS 8.2.1?", primary=("8.2.1 corrected the duplicate accession numbering bug",), answer=("duplicate",)),
        Query("paraphrase", "paraphrase", "Can I put a defrosted plasma tube back in the freezer?", primary=("must never be refrozen",), answer=("never",)),
        Query("synonym", "synonym", "How are blood samples shipped between hospitals?", primary=("contracted courier in insulated boxes",), answer=("courier",)),
        Query("identifier", "identifier_to_concept", "Run 88 aborted with FLOWCELL_PRESSURE_LOW. What should I do?",
              primary=("reseat the flow cell",), supporting=("reason=FLOWCELL_PRESSURE_LOW",), answer=("reseat",)),
        Query("multi-hop", "multi_hop", "Who leads the lab that owns the troponin assay?",
              primary=("cardiology core lab is led by Dr. Imani Okafor",), supporting=("troponin assay is owned by the cardiology core lab",),
              required=("cardiology core lab is led by Dr. Imani Okafor", "troponin assay is owned by the cardiology core lab"), answer=("okafor",)),
        Query("cross-doc", "cross_document", "How long are raw data and consent forms retained?",
              primary=("records.raw_data_retention_years: 10", "records.consent_form_retention_years: 25"), answer=("10", "25")),
        Query("unanswerable", "unanswerable", "What is the cafeteria menu on Fridays?", answer=()),
    ]  # fmt: skip
    return corpus
