# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for ecc.memory.v2, wikilinks, FTS5 index and RRF fusion."""

from pathlib import Path
from unittest.mock import patch
import pytest

from botex import memory
from botex.ops.migrate_projects import SENTINEL_FILENAME, main, migrate_memory_dir


@pytest.fixture(autouse=True)
def cleanup_memory_cache():
    """Reset memory index cache after each test to prevent SQLite file locks."""
    yield
    memory.reset_memory_index_cache()


@pytest.fixture
def memory_dirs(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_memory"
    global_dir.mkdir()

    with (
        patch("botex.memory.get_global_memory_dir", return_value=global_dir),
        patch("botex.memory.get_swarm_memory_dir", return_value=None),
    ):
        yield ws_dir, global_dir


def test_wikilinks_extraction():
    """Verify regex extraction of Obsidian-style wikilinks."""
    text = "Check [[auth/jwt]] and [[ref_doc|Custom Title]] and repeated [[auth/jwt]]."
    links = memory.extract_wikilinks(text)
    assert links == ["auth/jwt", "ref_doc"]


def test_save_memory_v2_with_wikilinks_and_status(memory_dirs):
    """Verify ecc.memory.v2 saving auto-extracts links and supports status."""
    ws_dir, _ = memory_dirs
    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Architecture Decision: Modular Vault",
        content="This links to [[mem_arch_01]] and [[mem_arch_02]].",
        kind="decision",
        tags=["vault", "v2"],
        status="active",
        compiled_truth=True,
    )
    assert res["ok"] is True
    assert res["links"] == ["mem_arch_01", "mem_arch_02"]
    assert res["status"] == "active"
    assert res["compiled_truth"] is True

    # Read back
    read_res = memory.read_memory(workspace_dir=str(ws_dir), memory_id=res["id"])
    assert read_res["ok"] is True
    meta = read_res["metadata"]
    assert meta["schema"] == "ecc.memory.v2"
    assert meta["links"] == ["mem_arch_01", "mem_arch_02"]
    assert meta["status"] == "active"
    assert meta["compiled_truth"] is True


def test_fts5_indexing_and_search(memory_dirs):
    """Verify SQLite WAL FTS5 full-text indexing and fast search."""
    ws_dir, _ = memory_dirs
    m1 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Database Optimization",
        content="PostgreSQL connection pooling configuration with PgBouncer.",
        kind="context",
        tags=["db", "postgres"],
    )
    m2 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Frontend Styling",
        content="Tailwind CSS design tokens and responsive flexbox layouts.",
        kind="context",
        tags=["frontend", "css"],
    )

    # Search with keyword matching PostgreSQL
    res_db = memory.search_memory(workspace_dir=str(ws_dir), query="PostgreSQL")
    assert res_db["ok"] is True
    assert res_db["count"] >= 1
    ids = [m["id"] for m in res_db["memories"]]
    assert m1["id"] in ids
    assert m2["id"] not in ids

    # Search with keyword matching flexbox
    res_fe = memory.search_memory(workspace_dir=str(ws_dir), query="flexbox")
    assert res_fe["ok"] is True
    assert res_fe["count"] >= 1
    ids_fe = [m["id"] for m in res_fe["memories"]]
    assert m2["id"] in ids_fe
    assert m1["id"] not in ids_fe


def test_reciprocal_rank_fusion():
    """Verify RRF rank aggregation logic."""
    fts_ranks = {"docA": 0, "docB": 1, "docC": 2}
    vec_ranks = {"docB": 0, "docD": 1, "docA": 2}

    merged = memory.reciprocal_rank_fusion(fts_ranks, vec_ranks, k=60)
    # docB is rank 1 in FTS and rank 0 in Vec -> 1/61 + 1/60 = 0.01639 + 0.01667 = 0.03306 (highest)
    assert merged[0][0] == "docB"
    ids = [item[0] for item in merged]
    assert set(ids) == {"docA", "docB", "docC", "docD"}


def test_implicit_linker_and_multi_token_search(memory_dirs):
    """Verify bare/backticked IDs auto-link to existing notes and multi-token search works."""
    ws_dir, _ = memory_dirs

    target = memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_base_arch01",
        title="Base Architecture Spec",
        content="Core specification for token validation and retry backoff.",
        kind="decision",
    )
    assert target["ok"] is True

    # Save a second note mentioning `mem_base_arch01` in inline code without [[wikilinks]]
    child = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Followup Implementation",
        content="Implemented changes according to `mem_base_arch01` specification.",
        kind="handoff",
    )
    assert child["ok"] is True
    assert "mem_base_arch01" in child["links"]

    # Backlinks should immediately report child -> mem_base_arch01
    bl = memory.get_backlinks(workspace_dir=str(ws_dir), memory_id="mem_base_arch01")
    assert bl["ok"] is True
    assert any(b["id"] == child["id"] for b in bl["backlinks"])

    # Multi-token out-of-order search fallback
    s_res = memory.search_memory(workspace_dir=str(ws_dir), query="backoff specification")
    assert s_res["ok"] is True
    assert s_res["count"] >= 1
    assert any(m["id"] == "mem_base_arch01" for m in s_res["memories"])


# ---------------------------------------------------------------------------
# Project Partitioning (Memory Vault v2)
# ---------------------------------------------------------------------------


def test_save_and_read_project_metadata(memory_dirs):
    """save_memory(project=...) normalizes to a slug and round-trips via read_memory."""
    ws_dir, _ = memory_dirs
    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Recall Pipeline Decision",
        content="Tiered recall synthesis for the harness.",
        kind="decision",
        project="Botex Harness",
    )
    assert res["ok"] is True
    assert res["project"] == "botex-harness"

    read_res = memory.read_memory(workspace_dir=str(ws_dir), memory_id=res["id"])
    assert read_res["ok"] is True
    assert read_res["metadata"]["project"] == "botex-harness"


def test_resave_without_project_preserves_frontmatter(memory_dirs):
    """Resaving a note without project= must preserve the stored project slug."""
    ws_dir, _ = memory_dirs
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_proj_keep",
        title="Partitioned Note",
        content="Initial body.",
        project="Botex Harness",
    )

    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_proj_keep",
        title="Partitioned Note v2",
        content="Updated body.",
    )
    assert res["ok"] is True
    assert res["project"] == "botex-harness"

    meta = memory.read_memory(workspace_dir=str(ws_dir), memory_id="mem_proj_keep")["metadata"]
    assert meta["project"] == "botex-harness"

    # An explicit project override still wins on resave.
    res2 = memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_proj_keep",
        title="Partitioned Note v3",
        content="Reassigned body.",
        project="media-gen",
    )
    assert res2["project"] == "media-gen"


def test_search_memory_project_filters(memory_dirs):
    """project= filters hits; project='_unassigned' selects notes with no project."""
    ws_dir, _ = memory_dirs
    m1 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Alpha Note",
        content="alpha body",
        project="botex-harness",
    )
    m2 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Beta Note",
        content="beta body",
        project="media-gen",
    )
    m3 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Gamma Note",
        content="gamma body",
    )

    res = memory.search_memory(workspace_dir=str(ws_dir), query="", project="botex-harness")
    assert res["ok"] is True
    ids = {m["id"] for m in res["memories"]}
    assert ids == {m1["id"]}
    assert res["memories"][0]["project"] == "botex-harness"

    res_un = memory.search_memory(workspace_dir=str(ws_dir), query="", project="_unassigned")
    assert res_un["ok"] is True
    ids_un = {m["id"] for m in res_un["memories"]}
    assert ids_un == {m3["id"]}
    assert res_un["memories"][0]["project"] == ""

    # Project filter normalization is case-insensitive slug matching.
    res_norm = memory.search_memory(workspace_dir=str(ws_dir), query="", project="Media-Gen")
    assert {m["id"] for m in res_norm["memories"]} == {m2["id"]}

    # Project filter composes with a keyword query.
    res_q = memory.search_memory(workspace_dir=str(ws_dir), query="alpha", project="botex-harness")
    assert {m["id"] for m in res_q["memories"]} == {m1["id"]}


def test_list_memory_projects_aggregation(memory_dirs):
    """Aggregates distinct projects with counts across local+global, deduplicating IDs."""
    ws_dir, _ = memory_dirs
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_a1",
        title="A1",
        content="x",
        project="botex-harness",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_a2",
        title="A2",
        content="x",
        project="Botex Harness",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_b1",
        title="B1",
        content="x",
        project="media-gen",
    )
    memory.save_memory(workspace_dir=str(ws_dir), memory_id="mem_u1", title="U1", content="x")
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_g1",
        title="G1",
        content="x",
        project="media-gen",
        scope="global",
    )
    # Same ID in both scopes is counted once (the local copy wins the shadow).
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_dup",
        title="DUP",
        content="x",
        project="botex-harness",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id="mem_dup",
        title="DUP",
        content="x",
        project="swarm-skills",
        scope="global",
    )

    res = memory.list_memory_projects(workspace_dir=str(ws_dir), scope="all")
    assert res["ok"] is True
    counts = {p["project"]: p["count"] for p in res["projects"]}
    assert counts["botex-harness"] == 3
    assert counts["media-gen"] == 2
    assert counts[""] == 1
    assert "swarm-skills" not in counts
    assert res["total"] == 6
    # The unassigned bucket sorts last.
    assert res["projects"][-1]["project"] == ""

    res_local = memory.list_memory_projects(workspace_dir=str(ws_dir), scope="local")
    counts_local = {p["project"]: p["count"] for p in res_local["projects"]}
    assert counts_local == {"botex-harness": 3, "media-gen": 1, "": 1}


# ---------------------------------------------------------------------------
# ops/migrate_projects.py — vault backfill migration
# ---------------------------------------------------------------------------


def _write_legacy_note(
    mem_dir: Path, stem: str, frontmatter: str, body: str = "Body text."
) -> Path:
    note = mem_dir / f"{stem}.md"
    note.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return note


def _note_project(note: Path) -> str:
    meta, _ = memory._parse_frontmatter(note.read_text(encoding="utf-8"))
    return str(meta.get("project") or "")


def test_migrate_memory_dir_backfill_and_sentinel(tmp_path):
    """Backfills project frontmatter, writes sentinel, and short-circuits on rerun."""
    mem_dir = tmp_path / "vault"
    mem_dir.mkdir()
    _write_legacy_note(
        mem_dir,
        "mem_legacy",
        "id: mem_legacy\ntitle: BoteX Curator Status\nkind: context\ntags:\n  - botex\n  - curator",
        "Curator health telemetry for the harness.",
    )
    _write_legacy_note(
        mem_dir,
        "mem_tagged",
        "id: mem_tagged\ntitle: Custom App Note\ntags: [project:custom-app]",
    )
    _write_legacy_note(
        mem_dir,
        "mem_set",
        "id: mem_set\ntitle: Media Note\nproject: media-gen",
    )
    plain = mem_dir / "mem_plain.md"
    plain.write_text("No frontmatter at all.\n", encoding="utf-8")

    res = migrate_memory_dir(mem_dir)
    assert res["ok"] is True
    assert res["skipped"] is False
    assert res["updated"] == 2
    assert (mem_dir / SENTINEL_FILENAME).exists()

    assert _note_project(mem_dir / "mem_legacy.md") == "botex"
    assert _note_project(mem_dir / "mem_tagged.md") == "custom-app"
    assert _note_project(mem_dir / "mem_set.md") == "media-gen"
    assert "project:" not in plain.read_text(encoding="utf-8")

    res2 = migrate_memory_dir(mem_dir)
    assert res2["ok"] is True
    assert res2["skipped"] is True
    assert res2["updated"] == 0


def test_migrate_memory_dir_dry_run(tmp_path):
    """dry_run classifies notes without writing files or the sentinel."""
    mem_dir = tmp_path / "vault"
    mem_dir.mkdir()
    note = _write_legacy_note(
        mem_dir,
        "mem_dry",
        "id: mem_dry\ntitle: Witcher3 Scene\ntags: [tw3se]",
        "w2scene composition notes.",
    )
    before = note.read_text(encoding="utf-8")

    res = migrate_memory_dir(mem_dir, dry_run=True)
    assert res["ok"] is True
    assert res["skipped"] is False
    assert res["updated"] == 1
    assert res["projects"].get("witcher3") == 1
    assert note.read_text(encoding="utf-8") == before
    assert not (mem_dir / SENTINEL_FILENAME).exists()


def test_migrate_projects_cli_dry_run(tmp_path, capsys):
    """CLI entry point resolves workspace scope dirs and honours --dry-run."""
    ws = tmp_path / "ws"
    mem_dir = ws / ".botex" / "memory"
    mem_dir.mkdir(parents=True)
    _write_legacy_note(
        mem_dir,
        "mem_cli",
        "id: mem_cli\ntitle: Prompt Improver Pipeline",
        "prompt-improver rollout.",
    )

    rc = main(["--workspace", str(ws), "--dry-run"])
    assert rc == 0
    assert not (mem_dir / SENTINEL_FILENAME).exists()
    assert _note_project(mem_dir / "mem_cli.md") == ""
    out = capsys.readouterr().out
    assert "[local]" in out
    assert "updated=1" in out


def test_search_memory_status_quarantine_and_opt_out(memory_dirs):
    """search_memory defaults to status='active', blocking candidate/deprecated/superseded."""
    ws_dir, _ = memory_dirs
    assert "candidate" in memory.VALID_STATUSES
    assert memory._VALID_STATUSES_MAP["candidate"] == "candidate"

    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Active Anchor",
        content="Routing bugfix guide linking [[mem_cand]] and [[mem_dep]].",
        kind="context",
        memory_id="mem_anchor",
        status="active",
        links=["mem_cand", "mem_dep"],
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Candidate Note",
        content="Unvetted candidate lesson about routing bugfix.",
        kind="lesson",
        memory_id="mem_cand",
        status="candidate",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Deprecated Note",
        content="Old deprecated routing bugfix note.",
        kind="lesson",
        memory_id="mem_dep",
        status="deprecated",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Superseded Note",
        content="Superseded routing bugfix note.",
        kind="lesson",
        memory_id="mem_sup",
        status="superseded",
    )

    # Default search hides candidate, deprecated, and superseded — even across RRF neighbors.
    res_active = memory.search_memory(workspace_dir=str(ws_dir), query="routing bugfix")
    assert res_active["ok"] is True
    active_ids = {m["id"] for m in res_active["memories"]}
    assert active_ids == {"mem_anchor"}

    # Explicit candidate filter and list_memories forwarding work.
    res_cand = memory.search_memory(workspace_dir=str(ws_dir), query="", status="candidate")
    assert {m["id"] for m in res_cand["memories"]} == {"mem_cand"}
    listed_cand = memory.list_memories(workspace_dir=str(ws_dir), status="candidate")
    assert {m["id"] for m in listed_cand["memories"]} == {"mem_cand"}

    # Opt-outs ('all', '', None) return every lifecycle state.
    for opt_out in ("all", "", None):
        res_all = memory.search_memory(workspace_dir=str(ws_dir), query="", status=opt_out)
        assert res_all["ok"] is True
        assert {m["id"] for m in res_all["memories"]} == {
            "mem_anchor",
            "mem_cand",
            "mem_dep",
            "mem_sup",
        }


def test_search_memory_rejects_invalid_and_bool_status(memory_dirs):
    """Invalid status strings and bool status values must fail cleanly rather than disabling filter."""
    ws_dir, _ = memory_dirs
    for bad in ("bogus", False, True):
        res = memory.search_memory(workspace_dir=str(ws_dir), query="", status=bad)
        assert res["ok"] is False
        assert "status" in res["error"].lower()
