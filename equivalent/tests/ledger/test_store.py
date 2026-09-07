import threading
import multiprocessing

import pytest

from equivalent.ledger.records import Predicate, RequestLogLine
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject


def _tree(n=1):
    return Subject(kind="tree", sha256=f"{n:064d}")


def _pred(verdict="pass", config="cfg-1", tool="builder"):
    return Predicate(tool=tool, version="0.1.0", configHash=config, verdict=verdict, detail={})


def _process_writer(path, worker_id):
    store = LedgerStore(path)
    for i in range(10):
        store.record_claim(
            [_tree(1)], "build/replay", _pred(config=f"{worker_id}-{i}"), [],
            f"process-{worker_id}",
        )


def test_append_claim_never_rewrites_earlier_bytes(tmp_path):
    store = LedgerStore(tmp_path / "region")
    store.record_claim([_tree(1)], "build/replay", _pred(), [], "sess-1")
    before = store.claims_path.read_bytes()
    store.record_claim([_tree(1)], "gpu/executed", _pred(), [], "sess-1")
    after = store.claims_path.read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)


def test_next_claim_id_is_a_per_region_counter(tmp_path):
    store = LedgerStore(tmp_path / "region")
    assert store.next_claim_id() == "c-0001"
    c1 = store.record_claim([_tree(1)], "build/replay", _pred(), [], "sess-1")
    assert c1.id == "c-0001"
    c2 = store.record_claim([_tree(1)], "gpu/executed", _pred(), [], "sess-1")
    assert c2.id == "c-0002"


def test_latest_returns_most_recent_claim_for_same_subject_and_config(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)
    store.append_claim(_claim_at(store, tree, "timing/port", _pred("pass"), ts="2026-01-01T00:00:00Z"))
    newest = _claim_at(store, tree, "timing/port", _pred("fail"), ts="2026-01-02T00:00:00Z")
    store.append_claim(newest)
    got = store.latest("timing/port", tree, required_materials=())
    assert got.id == newest.id
    assert got.predicate.verdict == "fail"


def test_latest_prefers_the_later_appended_claim_on_a_timestamp_tie(tmp_path):
    # Timestamps have one-second resolution, so a pass and a fail recorded
    # in the same second are a real case; the later line in the file wins.
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)
    ts = "2026-01-01T00:00:00Z"
    store.append_claim(_claim_at(store, tree, "build/replay", _pred("pass"), ts=ts))
    later = _claim_at(store, tree, "build/replay", _pred("fail"), ts=ts)
    store.append_claim(later)
    assert store.latest("build/replay", tree, required_materials=()).id == later.id


def test_a_reader_skips_a_torn_final_line_instead_of_crashing(tmp_path):
    # The CLI reads a ledger the gateway may be appending to from another
    # process; a final line whose newline hasn't landed yet is skipped.
    store = LedgerStore(tmp_path / "region")
    store.record_claim([_tree(1)], "build/replay", _pred(), [], "sess-1")
    with open(store.claims_path, "a") as f:
        f.write('{"id": "c-0002", "ts": "2026-')
    assert [c.id for c in store.all_claims()] == ["c-0001"]


def test_exists_pass_unaffected_by_later_fail_on_a_different_subject(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree1, tree2 = _tree(1), _tree(2)
    store.record_claim([tree1], "build/replay", _pred("pass"), [], "sess-1")
    store.record_claim([tree2], "build/replay", _pred("fail"), [], "sess-1")
    assert store.exists_pass("build/replay", tree1, required_materials=()) is True
    assert store.exists_pass("build/replay", tree2, required_materials=()) is False


def test_find_duplicate_matches_only_when_type_tree_and_config_all_equal(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)
    claim = store.record_claim([tree], "build/replay", _pred(config="cfg-A"), [], "sess-1")

    assert store.find_duplicate("build/replay", tree, "cfg-A", required_materials=()).id == claim.id
    assert store.find_duplicate("build/replay", tree, "cfg-B", required_materials=()) is None
    assert store.find_duplicate("build/replay", _tree(2), "cfg-A", required_materials=()) is None
    assert store.find_duplicate("gpu/executed", tree, "cfg-A", required_materials=()) is None


def test_duplicate_and_latest_require_current_materials(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)
    old_strategy = Subject(kind="strategy", sha256="a" * 64)
    new_strategy = Subject(kind="strategy", sha256="b" * 64)
    claim = store.record_claim(
        [tree], "build/replay", _pred(config="cfg-A"), [old_strategy], "sess-1",
    )

    assert store.find_duplicate(
        "build/replay", tree, "cfg-A", required_materials=[old_strategy],
    ) == claim
    assert store.find_duplicate(
        "build/replay", tree, "cfg-A", required_materials=[new_strategy],
    ) is None
    assert store.latest(
        "build/replay", tree, required_materials=[new_strategy],
    ) is None


def test_reading_a_claim_without_saying_which_context_is_a_mistake(tmp_path):
    # A claim is current evidence only relative to the materials it had to
    # be reached against, so a reader that does not say which those are is
    # asking a question with no answer.
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)
    store.record_claim([tree], "build/replay", _pred(), [], "sess-1")

    with pytest.raises(TypeError):
        store.latest("build/replay", tree)


def test_sequential_appends_do_not_interleave_partial_lines(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)

    def worker(i):
        for _ in range(20):
            store.record_claim([tree], "build/replay", _pred(config=f"cfg-{i}"), [], f"sess-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = store.claims_path.read_text().splitlines()
    assert len(lines) == 80
    import json
    for line in lines:
        json.loads(line)  # every line parses on its own; none is a partial write

    ids = [json.loads(line)["id"] for line in lines]
    assert len(ids) == len(set(ids))  # the lock also serialized id assignment


def test_separate_processes_cannot_allocate_the_same_claim_id(tmp_path):
    region = tmp_path / "region"
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_process_writer, args=(region, i)) for i in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    claims = LedgerStore(region).all_claims()
    assert len(claims) == 30
    assert len({claim.id for claim in claims}) == 30


def test_record_claim_assigns_non_decreasing_timestamps(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree = _tree(1)
    claims = [store.record_claim([tree], "build/replay", _pred(config=f"cfg-{i}"), [], "sess-1") for i in range(5)]
    timestamps = [c.ts for c in claims]
    assert timestamps == sorted(timestamps)


def test_put_artifact_is_idempotent(tmp_path):
    store = LedgerStore(tmp_path / "region")
    data = b"binary contents"
    sha = "deadbeef" * 8
    p1 = store.put_artifact(sha, data)
    p2 = store.put_artifact(sha, data)
    assert p1 == p2
    assert p1.read_bytes() == data


def test_append_request_records_refusal_and_duplicate_outcomes(tmp_path):
    store = LedgerStore(tmp_path / "region")
    refusal = RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="sess-1", model="claude-sonnet-5",
        endpoint="run", action="build_replay", region="ch04:step",
        tree="a" * 64, config_hash=None, outcome="refused",
        missing=({"predicateType": "sese/verified", "subject_kind": "frozen"},),
    )
    store.append_request(refusal)
    lines = store.requests_path.read_text().splitlines()
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["outcome"] == "refused"
    assert row["missing"][0]["predicateType"] == "sese/verified"


def test_list_trees_lists_each_tree_once(tmp_path):
    store = LedgerStore(tmp_path / "region")
    store.record_claim([_tree(1)], "build/replay", _pred(), [], "sess-1")
    store.record_claim([_tree(1)], "gpu/executed", _pred(), [], "sess-1")
    store.record_claim([_tree(2)], "build/replay", _pred(), [], "sess-1")
    assert sorted(store.list_trees()) == sorted([_tree(1).sha256, _tree(2).sha256])


def _claim_at(store, tree, predicate_type, predicate, ts):
    from equivalent.ledger.records import Claim
    return Claim(
        id=store.next_claim_id(),
        ts=ts,
        subject=(tree,),
        predicateType=predicate_type,
        predicate=predicate,
        materials=(),
        session="sess-1",
    )


def test_find_duplicate_has_a_docstring(tmp_path):
    # A string placed after the first statement of a function body is dead
    # code, not a docstring, so the help a reader asks for is not there.
    doc = LedgerStore.find_duplicate.__doc__
    assert doc is not None
    assert "predicate type" in doc


def test_recording_a_claim_with_an_unregistered_predicate_type_writes_nothing(tmp_path):
    # An unknown predicate type must be refused before the line is on disk;
    # otherwise the ledger keeps a claim nothing can read back.
    store = LedgerStore(tmp_path / "region")
    with pytest.raises(KeyError):
        store.record_claim([_tree(1)], "not/a/predicate", _pred(), [], "sess-1")
    assert not store.claims_path.exists()


def test_an_unregistered_predicate_type_leaves_earlier_claims_untouched(tmp_path):
    store = LedgerStore(tmp_path / "region")
    store.record_claim([_tree(1)], "build/replay", _pred(), [], "sess-1")
    before = store.claims_path.read_bytes()
    with pytest.raises(KeyError):
        store.record_claim([_tree(1)], "not/a/predicate", _pred(), [], "sess-1")
    assert store.claims_path.read_bytes() == before
