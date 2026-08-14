import json
from pathlib import Path
import pytest
from global_executables.model import shard, valid_command
from global_executables.pipeline import load_canonical, rebuild
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

def test_bad_record_fails(tmp_path):
    bad=tmp_path/"bad.jsonl"; bad.write_text('{"command":"bad/name","ecosystem":"npm","package":"x","version":null,"repository":null,"source":"x","confidence":"direct"}\n')
    with pytest.raises(ValueError): rebuild(tmp_path,[bad],"2026-08-14")
