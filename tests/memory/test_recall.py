"""Recall ranking: lexical, subject, one-hop expansion, pinned preferences, budgets."""

from hollis.memory import MemoryManager, RecallQuery, user_fact
from hollis.runtime.memory import MemoryKind
from tests.memory.conftest import ALICE, BOB, Clock

SCOPE = "user:alice"


async def _seed(manager: MemoryManager) -> None:
    facts = [
        ("Payments service runs on Kubernetes in eu-central-1", "payments", "platform"),
        ("Payments service owner is the Falcon team", "payments", "owner"),
        ("The Falcon team's on-call rotation changes on Mondays", "falcon team", "oncall"),
        ("Search service uses OpenSearch 2.11", "search", "engine"),
        ("Office plants are watered on Fridays", None, None),
    ]
    for content, subject, key in facts:
        await manager.remember(user_fact(SCOPE, content, subject=subject, key=key), ALICE)


async def test_lexical_and_subject_matches_rank_first(manager: MemoryManager) -> None:
    await _seed(manager)
    recalled = await manager.recall(
        RecallQuery(text="who owns the payments service?", scopes=ALICE.read, limit=3)
    )
    contents = [m.content for m in recalled]
    assert contents[0] == "Payments service owner is the Falcon team"
    assert "Office plants are watered on Fridays" not in contents


async def test_one_hop_expansion_reaches_related_subjects(manager: MemoryManager) -> None:
    await _seed(manager)
    recalled = await manager.recall(RecallQuery(text="payments", scopes=ALICE.read, limit=5))
    contents = [m.content for m in recalled]
    assert "Payments service runs on Kubernetes in eu-central-1" in contents
    assert "Payments service owner is the Falcon team" in contents


async def test_explicit_subject_filter(manager: MemoryManager) -> None:
    await _seed(manager)
    recalled = await manager.recall(
        RecallQuery(text="details", scopes=ALICE.read, subject="Search")
    )
    assert next(m.content for m in recalled) == "Search service uses OpenSearch 2.11"


async def test_preferences_are_pinned_whatever_the_query(manager: MemoryManager) -> None:
    await _seed(manager)
    await manager.remember(
        user_fact(SCOPE, "Always answer in British English", kind=MemoryKind.PREFERENCE), ALICE
    )
    recalled = await manager.recall(
        RecallQuery(text="search engine version", scopes=ALICE.read, limit=3)
    )
    assert "Always answer in British English" in [m.content for m in recalled]


async def test_scope_isolation_and_shared_scopes(manager: MemoryManager) -> None:
    await manager.remember(
        user_fact("user:bob", "Bob's salary is confidential", subject="bob"), BOB
    )
    await manager.remember(user_fact(SCOPE, "Alice reviews salary bands"), ALICE)
    recalled = await manager.recall(RecallQuery(text="salary", scopes=ALICE.read))
    assert [m.content for m in recalled] == ["Alice reviews salary bands"]


async def test_character_budget_and_limit(manager: MemoryManager) -> None:
    await _seed(manager)
    recalled = await manager.recall(
        RecallQuery(text="payments falcon search", scopes=ALICE.read, max_chars=200)
    )
    assert sum(len(m.render()) for m in recalled) <= 200
    assert recalled


async def test_recall_updates_usage(manager: MemoryManager, clock: Clock) -> None:
    await _seed(manager)
    recalled = await manager.recall(RecallQuery(text="opensearch", scopes=ALICE.read, limit=1))
    stored = await manager.store.get(recalled[0].memory_id)
    assert stored is not None
    assert stored.recall_count == 1
    assert stored.last_recalled_at == clock.now


async def test_empty_scopes_recall_nothing(manager: MemoryManager) -> None:
    await _seed(manager)
    assert await manager.recall(RecallQuery(text="payments", scopes=())) == ()
