import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.runtime.events import serialize_event
from hollis.runtime.model import FailureKind, ModelCallError, ModelResponse
from hollis.runtime.policy import RunBudget
from hollis.runtime.redaction import REDACTED, Redactor
from hollis.runtime.runner import Runner, RunSpec, SecretInRunInput
from hollis.runtime.state import RunStatus

KEY = "sk-ant-api03-PLANTEDsecretVALUE-0123456789"
SPEC = RunSpec(model="fake", input="hello", budget=RunBudget(max_output_tokens=64, max_attempts=2))


async def _no_sleep(_: float) -> None:
    return None


# --- Redactor ------------------------------------------------------------------------------


def test_known_values_are_replaced_everywhere() -> None:
    redactor = Redactor([SecretStr(KEY), "another-secret-value"])
    text = f"a {KEY} b another-secret-value c {KEY}"
    assert redactor.redact_known(text) == f"a {REDACTED} b {REDACTED} c {REDACTED}"
    assert redactor.contains_secret(text)
    assert not redactor.contains_secret("nothing here")


def test_longer_secret_containing_a_shorter_one_is_replaced_whole() -> None:
    redactor = Redactor(["abcdefgh", "abcdefgh-and-more"])
    assert redactor.redact_known("x abcdefgh-and-more y") == f"x {REDACTED} y"


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        Redactor(["short"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("key sk-ant-api03-abcdefghijkl leaked", f"key {REDACTED} leaked"),
        ("Authorization: Bearer abc.def-ghi_jkl", f"Authorization: {REDACTED}"),
        ("sent x-api-key: hunter2hunter2", f"sent x-api-key: {REDACTED}"),
        ("api_key=abc123456789, next", f"api_key={REDACTED}, next"),
        ("plain diagnostic text", "plain diagnostic text"),
    ],
)
def test_credential_patterns_are_redacted_in_diagnostics(text: str, expected: str) -> None:
    assert Redactor().redact(text) == expected


def test_redaction_is_idempotent() -> None:
    redactor = Redactor([KEY])
    once = redactor.redact(f"api_key: {KEY} and Bearer abcdefghijkl")
    assert redactor.redact(once) == once


def test_patterns_are_not_applied_to_known_value_redaction() -> None:
    # Model content may legitimately discuss key formats; only registered values are removed.
    assert Redactor().redact_known("keys look like sk-ant-api03-abcdefghijkl") == (
        "keys look like sk-ant-api03-abcdefghijkl"
    )


# --- runtime integration -------------------------------------------------------------------


async def test_input_containing_a_registered_secret_is_rejected_before_persisting(
    tmp_path: Path,
) -> None:
    store = await SqliteRunStore.open(tmp_path / "runs.db")
    runner = Runner(store, ScriptedModelProvider([]), redactor=Redactor([KEY]))
    for spec in (
        SPEC.model_copy(update={"input": f"my key is {KEY}"}),
        SPEC.model_copy(update={"instructions": f"use {KEY}"}),
    ):
        with pytest.raises(SecretInRunInput):
            await runner.create(spec)
    assert KEY.encode() not in (tmp_path / "runs.db").read_bytes()


async def test_planted_secret_never_reaches_events_database_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider echoes the key in an error message and in response content."""
    caplog.set_level(logging.DEBUG, logger="hollis")
    db = tmp_path / "runs.db"
    store = await SqliteRunStore.open(db)
    provider = ScriptedModelProvider(
        [
            ModelCallError(FailureKind.OVERLOADED, f"upstream said x-api-key: {KEY}"),
            ModelResponse(content=f"echoing {KEY} back"),
        ],
        per_attempt=True,
    )
    runner = Runner(store, provider, redactor=Redactor([KEY]), sleep=_no_sleep)

    run = await runner.create(SPEC)
    final = await runner.execute(run.run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response is not None
    assert final.response.content == f"echoing {REDACTED} back"
    serialized = "".join(serialize_event(e) for e in await store.read(run.run_id))
    assert KEY not in serialized
    assert REDACTED in serialized
    assert KEY.encode() not in db.read_bytes()
    assert caplog.records, "expected the runtime to log the failed attempt"
    assert all(KEY not in record.getMessage() for record in caplog.records)


async def test_run_failure_message_is_redacted(tmp_path: Path) -> None:
    store = await SqliteRunStore.open(tmp_path / "runs.db")
    provider = ScriptedModelProvider(
        [ModelCallError(FailureKind.AUTHENTICATION, f"invalid key {KEY}")]
    )
    runner = Runner(store, provider, redactor=Redactor([KEY]))
    final = await runner.execute((await runner.create(SPEC)).run_id)
    assert final.failure is not None
    assert final.failure.message == f"invalid key {REDACTED}"
    assert final.last_failure is not None
    assert KEY not in final.last_failure.message
