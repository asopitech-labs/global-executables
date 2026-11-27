import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from global_executables.model import shard, valid_command
from global_executables.pipeline import DatasetShrinkError, RebuildPolicy, load_canonical, rebuild, sync_npm_coverage
from global_executables.search import Dataset, DatasetIndexError

ROOT=Path(__file__).parents[1]
INPUTS=sorted((ROOT/"fixtures/intermediate").glob("*.jsonl"))

def test_names_and_sharding():
    assert valid_command("git") and valid_command("Git") and not valid_command("a/b")
    assert shard("envcp")=="en" and shard("☃").startswith("_")

def test_build_merge_indexes_and_incremental_equivalence(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14")
    first={p.relative_to(tmp_path):p.read_text() for p in (tmp_path/"data").glob("**/*") if p.is_file() and p.name!="metadata.json"}
    records=load_canonical(tmp_path)
    metadata=json.loads((tmp_path/"data/metadata.json").read_text())
    assert metadata["unique_executables"] == len(records)
    assert metadata["unique_executables"] == sum(1 for p in (tmp_path/"data/executables").glob("**/*.json"))
    assert len(records["envcp"]["providers"])==2
    assert records["foocli"]["providers"][0]["package"]=="foo-tool"
    rebuild(tmp_path,INPUTS,"2026-08-14")
    second={p.relative_to(tmp_path):p.read_text() for p in (tmp_path/"data").glob("**/*") if p.is_file() and p.name!="metadata.json"}
    assert first==second

def test_search_semantics_and_similarity(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14"); d=Dataset(tmp_path)
    assert d.check("envcp")["status"]=="collision"
    assert d.check("evpk")["status"]=="unknown"
    assert d.check("envcp")["snapshot"]=="2026-08-14"
    assert any(x["name"]=="envcp" for x in d.similar("envc"))

def test_only_explicitly_exhaustive_snapshot_supports_negative_result(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14",coverage_kind="exhaustive")
    assert Dataset(tmp_path).check("evpk")["status"]=="clear_in_index"

def test_rebuild_accepts_per_source_coverage_kinds(tmp_path):
    rebuild(tmp_path, INPUTS, "2026-08-14", coverage_kind={"debian": "exhaustive", "npm": "partial"})
    metadata = json.loads((tmp_path / "data/metadata.json").read_text())
    assert metadata["coverage"]["debian"]["coverage_kind"] == "exhaustive"
    assert metadata["coverage"]["npm"]["coverage_kind"] == "partial"
    assert metadata["negative_lookup"] == "unknown"


def test_partial_source_preserves_unobserved_history_and_replaces_observed_provider(tmp_path):
    first = tmp_path / "npm.jsonl"
    first.write_text(json.dumps({
        "command": "partial-tool", "ecosystem": "npm", "package": "partial-tool",
        "version": "1", "repository": None, "source": "catalog-v1", "confidence": "direct",
    }) + "\n")
    rebuild(tmp_path, [first], "2026-08-14", coverage_kind={"npm": "partial"})

    first.write_text("")
    rebuild(tmp_path, [first], "2026-08-15", coverage_kind={"npm": "partial"})
    preserved = load_canonical(tmp_path)["partial-tool"]
    assert preserved["last_seen"] == "2026-08-14"
    assert preserved["providers"][0]["version"] == "1"

    first.write_text(json.dumps({
        "command": "partial-tool", "ecosystem": "npm", "package": "partial-tool",
        "version": "2", "repository": None, "source": "catalog-v2", "confidence": "direct",
    }) + "\n")
    rebuild(tmp_path, [first], "2026-08-16", coverage_kind={"npm": "partial"})
    observed = load_canonical(tmp_path)["partial-tool"]
    assert observed["last_seen"] == "2026-08-16"
    assert [provider["version"] for provider in observed["providers"]] == ["2"]

def test_rebuild_preserves_declared_coverage_scope(tmp_path):
    npm_scope = {
        "coverage_kind": "exhaustive",
        "scope": "ecosyste.ms critical npm snapshot (80% of downloads)",
        "catalog_packages": 2295,
    }
    rebuild(tmp_path, INPUTS, "2026-08-14", coverage_kind={"npm": npm_scope})
    metadata = json.loads((tmp_path / "data/metadata.json").read_text())
    assert metadata["coverage"]["npm"] == {
        "status": "success",
        "records": 2,
        "source": str(ROOT / "fixtures/intermediate/npm.jsonl"),
        **npm_scope,
    }


def test_npm_catalog_completion_does_not_claim_registry_exhaustiveness(tmp_path):
    coverage = {"npm": {"coverage_kind": "exhaustive", "scope": "critical npm"}}
    assert sync_npm_coverage(coverage, {})["npm"]["coverage_kind"] == "partial"
    separate = sync_npm_coverage(coverage, {"sources": {"npm-registry": {
        "catalog_complete": True, "catalog_size": 4300000, "cursor": 221774,
        "catalog_source": "https://replicate.npmjs.com/_all_docs",
        "catalog_scope": "npm registry package-name index",
    }}})
    assert separate["npm-registry"]["catalog_packages"] == 4300000
    assert separate["npm-registry"]["catalog_complete"] is False
    assert separate["npm"]["catalog_complete"] is False
    incomplete = {"sources": {"npm": {"catalog_complete": True}}}
    assert sync_npm_coverage(coverage, incomplete)["npm"]["coverage_kind"] == "partial"
    complete = {"sources": {"npm": {
        "catalog_complete": True,
        "catalog_digest": "sha256:" + "a" * 64,
        "catalog_scope": "critical npm",
        "catalog_size": 2295,
        "catalog_snapshot": "2026-08-26",
        "catalog_source": "https://packages.ecosyste.ms/critical?registry=npmjs.org",
        "cursor": 2295,
        "failures": {},
        "retry_npm": [],
    }}}
    synchronized = sync_npm_coverage(coverage, complete)["npm"]
    assert synchronized["coverage_kind"] == "partial"
    assert synchronized["catalog_complete"] is True
    assert synchronized["catalog_packages"] == 2295
    rebuild(tmp_path, [ROOT / "fixtures/intermediate/npm.jsonl"], "2026-08-26",
            coverage_kind={"npm": synchronized})
    dataset = Dataset(tmp_path)
    assert dataset.metadata["negative_lookup"] == "unknown"
    assert dataset.check("missing-cli")["status"] == "unknown"
    for invalid in (0, True, None):
        complete["sources"]["npm"]["catalog_size"] = invalid
        invalid_coverage = sync_npm_coverage(coverage, complete)["npm"]
        assert invalid_coverage["coverage_kind"] == "partial"
        assert invalid_coverage["catalog_complete"] is False


def test_npm_registry_coverage_accepts_legacy_string_descriptor():
    coverage = {"npm": "partial", "npm-registry": "partial"}
    state = {"sources": {"npm-registry": {
        "catalog_size": 4300000,
        "cursor": 221774,
    }}}

    synchronized = sync_npm_coverage(coverage, state)

    assert synchronized["npm-registry"] == {
        "coverage_kind": "partial",
        "catalog_packages": 4300000,
        "catalog_complete": False,
    }

def test_indexed_search_matches_reference_scan_and_intersects_filters(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14")
    dataset=Dataset(tmp_path)
    records=load_canonical(tmp_path).values()
    expected=sorted(r["command"] for r in records if r["command"].startswith("e") and len(r["command"])==5 and any(p["ecosystem"]=="npm" for p in r["providers"]))
    assert dataset.search(prefix="e",length=5,ecosystem="npm")==expected

def test_query_reads_candidate_indexes_not_canonical_dataset(tmp_path, monkeypatch):
    rebuild(tmp_path,INPUTS,"2026-08-14")
    dataset=Dataset(tmp_path); reads=[]; original=dataset._read_index
    def measured(relative): reads.append(relative); return original(relative)
    monkeypatch.setattr(dataset,"_read_index",measured)
    assert dataset.search(prefix="env",length=5)==["envcp"]
    assert reads==["indexes/prefix/en.json","indexes/length/5.json"]
    reads.clear(); dataset.similar("envc")
    assert reads and all(path.startswith("indexes/trigram/") for path in reads)
    assert len(reads)<=len("  envc  ")-2

def test_missing_or_corrupt_manifested_index_fails_closed(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14"); dataset=Dataset(tmp_path)
    index=tmp_path/"data/indexes/prefix/en.json"
    index.unlink()
    with pytest.raises(DatasetIndexError,match="missing"): dataset.search(prefix="env")
    rebuild(tmp_path,INPUTS,"2026-08-14"); index.write_text('["tampered"]\n')
    with pytest.raises(DatasetIndexError,match="stale or corrupt"): Dataset(tmp_path).search(prefix="env")

def test_failed_coverage_is_unknown(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14",coverage_kind="exhaustive")
    p=tmp_path/"data/metadata.json"; m=json.loads(p.read_text()); m["coverage"]["pypi"]["status"]="failed"; p.write_text(json.dumps(m))
    assert Dataset(tmp_path).check("newx")["status"]=="unknown"

def test_bad_record_is_skipped_and_reported(tmp_path):
    bad=tmp_path/"bad.jsonl"; bad.write_text('{"command":"bad/name","ecosystem":"npm","package":"x","version":null,"repository":null,"source":"x","confidence":"direct"}\n')
    result = rebuild(tmp_path,[bad],"2026-08-14")

    assert result.input_records == 1
    assert result.unique_executables == 0
    assert result.rejected_records == 1
    assert result.rejected == ({"command": "bad/name", "ecosystem": "npm", "package": "x",
                                "reason": "invalid executable name"},)


def test_refresh_report_counts_rejected_records(tmp_path):
    bad=tmp_path/"bad.jsonl"; bad.write_text('{"command":".","ecosystem":"shell","package":"sh","source":"fixture"}\n')
    report=tmp_path/"refresh.json"
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/refresh.py"), str(bad), "--root", str(tmp_path),
         "--snapshot", "2026-08-21", "--report", str(report)],
        capture_output=True, text=True, env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    value = json.loads(report.read_text())
    assert value["status"] == "success"
    assert value["records"] == 1 and value["accepted_records"] == 0
    assert value["rejected_records"] == 1
    assert value["rejected"][0]["reason"] == "invalid executable name"


def test_structurally_corrupt_input_still_fails(tmp_path):
    bad=tmp_path/"bad.jsonl"; bad.write_text('{not json}\n')
    with pytest.raises(json.JSONDecodeError):
        rebuild(tmp_path,[bad],"2026-08-14")


def test_rebuild_refuses_an_unexplained_dictionary_shrink(tmp_path):
    first=tmp_path/"first.jsonl"
    first.write_text(json.dumps({"command":"keepme","ecosystem":"npm","package":"keepme",
                                 "version":"1","repository":None,"source":"fixture",
                                 "confidence":"direct"})+"\n")
    rebuild(tmp_path,[first],"2026-08-14")
    empty=tmp_path/"empty.jsonl"; empty.write_text("")

    with pytest.raises(DatasetShrinkError, match="1 to 0"):
        rebuild(tmp_path,[empty],"2026-08-15")
    assert load_canonical(tmp_path)["keepme"]["last_seen"] == "2026-08-14"

    result = rebuild(tmp_path,[empty],"2026-08-15",
                     policy=RebuildPolicy(shrink_reason="source retired in issue #123"))
    assert result.shrink_reason == "source retired in issue #123"
    assert result.unique_executables == 0


def test_disappearing_and_reappearing_provider_preserves_first_seen(tmp_path):
    first=tmp_path/"first.jsonl"; first.write_text(json.dumps({"command":"returnx","ecosystem":"npm","package":"returnx","version":"1","repository":None,"source":"fixture","confidence":"direct"})+"\n")
    rebuild(tmp_path,[first],"2026-08-14")
    disappeared=tmp_path/"disappeared.jsonl"; disappeared.write_text("")
    rebuild(tmp_path,[disappeared],"2026-08-15",
            policy=RebuildPolicy(shrink_reason="test provider disappearance"))
    assert Dataset(tmp_path).get("returnx") is None
    rebuild(tmp_path,[first],"2026-08-16")
    record=Dataset(tmp_path).get("returnx")
    assert record["first_seen"] == "2026-08-14" and record["last_seen"] == "2026-08-16"


def test_scope_indexes_filter_logical_provider_dimensions(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14")
    dataset=Dataset(tmp_path)
    assert dataset.search(scope={"language":"javascript"}) == ["envcp", "foocli"]
    assert dataset.check("envcp", {"language":"javascript", "distribution":"debian"})["found"] is False
    assert dataset.check("envcp", {"registry":"npm"})["found"] is True


def test_go_name_rule_matches_the_go_import_path_check():
    """The Go test reads the same fixture through module.CheckImportPath."""
    from global_executables.model import go_installable_command
    fixture=json.loads((ROOT/"internal/goproxy/testdata/command-names.json").read_text())
    assert [n for n in fixture["installable"] if not go_installable_command(n)] == []
    assert [n for n in fixture["uninstallable"] if go_installable_command(n)] == []


def _go_row(command, package="example.com/m"):
    return {"command":command,"ecosystem":"go","package":package,"version":"v1.0.0",
            "repository":None,"source":"fixture","confidence":"direct"}


def test_uninstallable_go_names_already_published_leave_without_a_shrink_reason(tmp_path):
    """Before #120 the Go walk published directory names such as " 01_facade" that
    `go install` cannot build; partial Go coverage kept them on every refresh."""
    first=tmp_path/"go.jsonl"
    first.write_text("".join(json.dumps(r)+"\n" for r in (
        _go_row("gofmt"), _go_row(" 01_facade"), _go_row("Zadatak 1"), _go_row("{}"))))
    pypi=tmp_path/"pypi.jsonl"
    pypi.write_text(json.dumps({"command":"{}","ecosystem":"pypi","package":"braces",
                                "version":"1","repository":None,"source":"fixture",
                                "confidence":"direct"})+"\n")
    # Publish the old state the way it was published before the rule existed.
    from global_executables import pipeline
    original=pipeline.installable_provider
    pipeline.installable_provider=lambda command, provider: True
    try:
        rebuild(tmp_path,[first,pypi],"2026-11-26",coverage_kind="partial")
    finally:
        pipeline.installable_provider=original
    assert {" 01_facade","Zadatak 1","{}","gofmt"} <= set(load_canonical(tmp_path))

    # The Go observations are unchanged (modules are not re-read), and the walk
    # may not even list the old rows: both cases must drop the names.
    for go_input in (first, tmp_path/"empty-go.jsonl"):
        if not go_input.exists():
            go_input.write_text(json.dumps(_go_row("gofmt"))+"\n")
        result=rebuild(tmp_path,[go_input,pypi],"2026-11-27",coverage_kind="partial")
        records=load_canonical(tmp_path)
        assert set(records) == {"gofmt","{}"}
        assert [p["ecosystem"] for p in records["{}"]["providers"]] == ["pypi"]
        assert result.unique_executables == 2 and result.shrink_reason is None


def test_uninstallable_go_rows_are_reported_as_rejected(tmp_path):
    rows=tmp_path/"go.jsonl"
    rows.write_text(json.dumps(_go_row("ok"))+"\n"+json.dumps(_go_row("tugas13 "))+"\n")
    result=rebuild(tmp_path,[rows],"2026-11-27")
    assert set(load_canonical(tmp_path)) == {"ok"}
    assert result.rejected_records == 1
    assert result.rejected[0]["command"] == "tugas13 "
    assert result.rejected[0]["reason"] == "not an installable command name for this ecosystem"


def test_a_real_shrink_is_still_refused_alongside_go_cleanup(tmp_path):
    first=tmp_path/"in.jsonl"
    first.write_text(json.dumps(_go_row("Zadatak 1"))+"\n"+json.dumps(
        {"command":"keepme","ecosystem":"npm","package":"keepme","version":"1",
         "repository":None,"source":"fixture","confidence":"direct"})+"\n")
    from global_executables import pipeline
    original=pipeline.installable_provider
    pipeline.installable_provider=lambda command, provider: True
    try:
        rebuild(tmp_path,[first],"2026-11-26",coverage_kind="exhaustive")
    finally:
        pipeline.installable_provider=original
    empty=tmp_path/"empty.jsonl"; empty.write_text("")
    with pytest.raises(DatasetShrinkError):
        rebuild(tmp_path,[empty],"2026-11-27",coverage_kind="exhaustive")
