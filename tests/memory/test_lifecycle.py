"""The memory lifecycle: governed writes, slots, temporal validity, deletion and retention."""

from datetime import timedelta

import pytest

from hollis.memory import (
    ChangeAction,
    ChangeKind,
    MemoryCandidate,
    MemoryManager,
    MemoryPolicy,
    MemoryStatus,
    MemoryStore,
    Provenance,
    RecallQuery,
    SourceKind,
    WriteAction,
    user_fact,
)
from hollis.runtime.memory import Certainty, MemoryKind
from hollis.runtime.redaction import Redactor
from tests.memory.conftest import ALICE, BOB, T0, Clock

SCOPE = "user:alice"


def _agent(content: str, **fields: object) -> MemoryCandidate:
    return MemoryCandidate.model_validate(
        {
            "scope": SCOPE,
            "kind": MemoryKind.FACT,
            "content": content,
            "certainty": Certainty.CONFIRMED,  # requested; the policy caps it
            "provenance": Provenance(kind=SourceKind.AGENT, reference="run/turn/0/call/0"),
        }
        | fields
    )


async def _recall(manager: MemoryManager, text: str, **kw: object) -> list[str]:
    query = RecallQuery.model_validate({"text": text, "scopes": ALICE.read} | kw)
    return [m.content for m in await manager.recall(query)]


async def test_writes_need_a_writable_scope(manager: MemoryManager) -> None:
    outcome = await manager.remember(user_fact("user:bob", "Bob likes tea"), ALICE)
    assert outcome.action is WriteAction.REJECTED
    assert "not writable" in outcome.reason
    shared = await manager.remember(user_fact("workspace:acme", "Deploys on Tuesdays"), ALICE)
    assert shared.action is WriteAction.REJECTED  # readable is not writable


async def test_certainty_is_capped_by_source(manager: MemoryManager) -> None:
    agent = await manager.remember(_agent("The API is rate limited to 100 rps"), ALICE)
    assert agent.record is not None
    assert agent.record.certainty is Certainty.INFERRED
    external = await manager.remember(
        _agent(
            "Status page says all systems normal",
            provenance=Provenance(kind=SourceKind.TOOL, external=True),
        ),
        ALICE,
    )
    assert external.record is not None
    assert external.record.certainty is Certainty.OBSERVED
    user = await manager.remember(user_fact(SCOPE, "I work in UTC+2"), ALICE)
    assert user.record is not None
    assert user.record.certainty is Certainty.CONFIRMED


async def test_credentials_are_refused(memory_store: MemoryStore, clock: Clock) -> None:
    secret = "ghp_registeredSecretValue1234567890"
    manager = MemoryManager(memory_store, redactor=Redactor([secret]), clock=clock)
    registered = await manager.remember(user_fact(SCOPE, f"my token is {secret}"), ALICE)
    assert registered.action is WriteAction.REJECTED
    pattern = await manager.remember(
        user_fact(SCOPE, "use Authorization: Bearer abcdef0123456789"), ALICE
    )
    assert pattern.action is WriteAction.REJECTED
    assert await memory_store.count(SCOPE) == 0


async def test_policy_limits_kinds_and_size(memory_store: MemoryStore, clock: Clock) -> None:
    policy = MemoryPolicy(
        max_content_chars=20,
        allowed_kinds={SourceKind.AGENT: frozenset({MemoryKind.OBSERVATION})},
    )
    manager = MemoryManager(memory_store, policy=policy, clock=clock)
    assert (await manager.remember(_agent("short"), ALICE)).action is WriteAction.REJECTED
    long = _agent("x" * 30, kind=MemoryKind.OBSERVATION)
    assert (await manager.remember(long, ALICE)).action is WriteAction.REJECTED


async def test_exact_repeats_are_reinforced_and_can_be_confirmed(manager: MemoryManager) -> None:
    first = await manager.remember(_agent("Builds run on Linux", subject="ci", key="os"), ALICE)
    again = await manager.remember(_agent("builds  run on LINUX", subject="CI", key="os"), ALICE)
    confirmed = await manager.remember(
        user_fact(SCOPE, "Builds run on Linux", subject="ci", key="os"), ALICE
    )
    assert first.record is not None
    assert again.action is WriteAction.REINFORCED
    assert confirmed.action is WriteAction.REINFORCED
    assert confirmed.record is not None
    assert confirmed.record.memory_id == first.record.memory_id
    assert confirmed.record.certainty is Certainty.CONFIRMED
    assert confirmed.record.reinforced == 2


async def test_dedup_key_makes_writes_idempotent(manager: MemoryManager) -> None:
    candidate = _agent("Observed flaky test test_sync", dedup_key="tool:inv-1")
    first = await manager.remember(candidate, ALICE)
    retry = await manager.remember(candidate.model_copy(update={"content": "different"}), ALICE)
    assert retry.action is WriteAction.DUPLICATE
    assert retry.record == first.record


async def test_new_value_in_an_occupied_slot_is_a_conflict_not_an_overwrite(
    manager: MemoryManager,
) -> None:
    old = await manager.remember(
        user_fact(SCOPE, "Office is in Berlin", subject="alice", key="office"), ALICE
    )
    new = await manager.remember(
        user_fact(SCOPE, "Office is in Munich", subject="alice", key="office"), ALICE
    )
    assert old.record is not None
    assert new.record is not None
    assert new.action is WriteAction.CONFLICT
    assert new.record.conflicts_with == (old.record.memory_id,)
    recalled = await manager.recall(RecallQuery(text="where is the office", scopes=ALICE.read))
    by_content = {m.content: m for m in recalled}
    assert set(by_content) >= {"Office is in Berlin", "Office is in Munich"}
    assert by_content["Office is in Berlin"].conflicts_with == (new.record.memory_id,)
    assert "CONFLICTS with" in by_content["Office is in Munich"].render()


async def test_update_supersedes_and_the_past_stays_queryable(
    manager: MemoryManager, clock: Clock
) -> None:
    await manager.remember(
        user_fact(SCOPE, "Primary region is eu-west-1", subject="billing", key="region"), ALICE
    )
    before_move = clock.advance(days=10)
    clock.advance(days=1)
    update = await manager.remember(
        user_fact(
            SCOPE,
            "Primary region is eu-central-1",
            subject="billing",
            key="region",
            change=ChangeKind.UPDATE,
        ),
        ALICE,
    )
    assert update.action is WriteAction.SUPERSEDED
    assert await _recall(manager, "billing primary region") == ["Primary region is eu-central-1"]
    past = await _recall(manager, "billing primary region", as_of=before_move)
    assert past == ["Primary region is eu-west-1"]
    old_id = update.affected[0]
    history = await manager.history(old_id)
    assert [c.action for c in history] == [ChangeAction.CREATED, ChangeAction.SUPERSEDED]


async def test_correction_retracts_the_wrong_value_everywhere_in_time(
    manager: MemoryManager, clock: Clock
) -> None:
    await manager.remember(
        user_fact(SCOPE, "Alice's manager is Dana", subject="alice", key="manager"), ALICE
    )
    earlier = clock.advance(hours=1)
    clock.advance(hours=1)
    fix = await manager.remember(
        user_fact(
            SCOPE,
            "Alice's manager is Sam",
            subject="alice",
            key="manager",
            change=ChangeKind.CORRECTION,
        ),
        ALICE,
    )
    assert fix.action is WriteAction.CORRECTED
    assert await _recall(manager, "who is alice manager") == ["Alice's manager is Sam"]
    assert "Alice's manager is Dana" not in await _recall(manager, "alice manager", as_of=earlier)


async def test_an_inference_cannot_replace_a_confirmed_fact(manager: MemoryManager) -> None:
    await manager.remember(
        user_fact(
            SCOPE, "Prefers Python", kind=MemoryKind.PREFERENCE, subject="alice", key="language"
        ),
        ALICE,
    )
    attempt = await manager.remember(
        _agent(
            "Prefers Rust",
            kind=MemoryKind.PREFERENCE,
            subject="alice",
            key="language",
            change=ChangeKind.CORRECTION,
        ),
        ALICE,
    )
    assert attempt.action is WriteAction.CONFLICT
    contents = await _recall(manager, "language preference")
    assert "Prefers Python" in contents
    assert "Prefers Rust" in contents  # kept, linked, and marked inferred


async def test_temporary_condition_applies_then_expires(
    manager: MemoryManager, clock: Clock
) -> None:
    await manager.remember(
        user_fact(SCOPE, "Alice works from Berlin", subject="alice", key="location"), ALICE
    )
    trip = await manager.remember(
        user_fact(
            SCOPE,
            "Alice is in Tokyo this week",
            subject="alice",
            key="location",
            change=ChangeKind.TEMPORARY,
            valid_until=T0 + timedelta(days=7),
        ),
        ALICE,
    )
    assert trip.action is WriteAction.CREATED
    during = await _recall(manager, "where is alice location")
    assert during[0] == "Alice is in Tokyo this week"
    clock.advance(days=8)
    after = await _recall(manager, "where is alice location")
    assert after == ["Alice works from Berlin"]
    report = await manager.maintain()
    assert trip.record is not None
    assert report.expired == (trip.record.memory_id,)


async def test_forget_deletes_content_and_leaves_a_tombstone(
    manager: MemoryManager, memory_store: MemoryStore
) -> None:
    outcome = await manager.remember(
        user_fact(SCOPE, "My daughter's school is Lincoln Elementary"), ALICE
    )
    assert outcome.record is not None
    memory_id = outcome.record.memory_id
    with pytest.raises(LookupError):  # not even its existence is revealed to another scope
        await manager.forget(memory_id, BOB, reason="x", actor="bob")
    assert await manager.forget(memory_id, ALICE, reason="user asked", actor="alice")
    assert await memory_store.get(memory_id) is None
    assert await _recall(manager, "daughter school") == []
    history = await manager.history(memory_id)
    assert history[-1].action is ChangeAction.DELETED
    assert "Lincoln" not in " ".join(c.detail for c in history)


async def test_forget_subject_removes_every_version(manager: MemoryManager) -> None:
    await manager.remember(
        user_fact(SCOPE, "Project Falcon uses Go", subject="project falcon", key="lang"), ALICE
    )
    await manager.remember(
        user_fact(
            SCOPE,
            "Project Falcon uses Rust",
            subject="project falcon",
            key="lang",
            change=ChangeKind.UPDATE,
        ),
        ALICE,
    )
    deleted = await manager.forget_subject(
        SCOPE, "Project Falcon", ALICE, reason="cancelled", actor="alice"
    )
    assert deleted == 2
    assert await _recall(manager, "falcon") == []


async def test_retract_keeps_history_but_never_recalls(
    manager: MemoryManager, memory_store: MemoryStore
) -> None:
    outcome = await manager.remember(_agent("The cache TTL is 5 minutes"), ALICE)
    assert outcome.record is not None
    await manager.retract(outcome.record.memory_id, ALICE, reason="wrong", actor="alice")
    stored = await memory_store.get(outcome.record.memory_id)
    assert stored is not None
    assert stored.status is MemoryStatus.RETRACTED
    assert await _recall(manager, "cache TTL") == []


async def test_retention_is_conservative(
    manager: MemoryManager, clock: Clock, memory_store: MemoryStore
) -> None:
    stale = await manager.remember(
        _agent("Saw 3 retries in job 17", kind=MemoryKind.OBSERVATION), ALICE
    )
    used = await manager.remember(
        _agent("Saw slow disk on host db-2", kind=MemoryKind.OBSERVATION), ALICE
    )
    kept = await manager.remember(
        user_fact(SCOPE, "Never deploy on Fridays", kind=MemoryKind.OBSERVATION), ALICE
    )
    clock.advance(days=20)
    await _recall(manager, "slow disk db-2")  # usage keeps it
    clock.advance(days=15)
    report = await manager.maintain()
    assert stale.record
    assert used.record
    assert kept.record
    assert report.deleted == (stale.record.memory_id,)
    assert await memory_store.get(used.record.memory_id) is not None
    assert await memory_store.get(kept.record.memory_id) is not None  # confirmed: never deleted


async def test_scope_capacity_is_enforced(memory_store: MemoryStore, clock: Clock) -> None:
    manager = MemoryManager(memory_store, policy=MemoryPolicy(max_records_per_scope=2), clock=clock)
    assert (await manager.remember(user_fact(SCOPE, "one"), ALICE)).action is WriteAction.CREATED
    assert (await manager.remember(user_fact(SCOPE, "two"), ALICE)).action is WriteAction.CREATED
    full = await manager.remember(user_fact(SCOPE, "three"), ALICE)
    assert full.action is WriteAction.REJECTED
    assert "maximum" in full.reason


async def test_registered_secrets_are_refused_even_without_pattern_checks(
    memory_store: MemoryStore, clock: Clock
) -> None:
    secret = "opaque-registered-value-0123456789"
    manager = MemoryManager(
        memory_store,
        policy=MemoryPolicy(reject_credential_patterns=False),
        redactor=Redactor([secret]),
        clock=clock,
    )
    outcome = await manager.remember(user_fact(SCOPE, f"remember {secret}"), ALICE)
    assert outcome.action is WriteAction.REJECTED
    assert "registered credential" in outcome.reason
