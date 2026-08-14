import json
from pathlib import Path
import pytest
from global_executables.model import shard, valid_command
from global_executables.pipeline import load_canonical, rebuild
from global_executables.search import Dataset

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
    assert d.check("evpk")["status"]=="clear_in_index"
    assert d.check("envcp")["snapshot"]=="2026-08-14"
    assert any(x["name"]=="envcp" for x in d.similar("envc"))

def test_failed_coverage_is_unknown(tmp_path):
    rebuild(tmp_path,INPUTS,"2026-08-14")
    p=tmp_path/"data/metadata.json"; m=json.loads(p.read_text()); m["coverage"]["pypi"]["status"]="failed"; p.write_text(json.dumps(m))
    assert Dataset(tmp_path).check("newx")["status"]=="unknown"

def test_bad_record_fails(tmp_path):
    bad=tmp_path/"bad.jsonl"; bad.write_text('{"command":"bad/name","ecosystem":"npm","package":"x","version":null,"repository":null,"source":"x","confidence":"direct"}\n')
    with pytest.raises(ValueError): rebuild(tmp_path,[bad],"2026-08-14")
