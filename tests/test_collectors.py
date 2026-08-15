import json
from pathlib import Path
from jsonschema import Draft202012Validator
from global_executables.collectors import crates_manifest, homebrew_metadata, npm_metadata, package_files
ROOT=Path(__file__).parents[1]/"fixtures/collectors"

def test_filesystem_collectors_only_bin_paths():
    deb=package_files((ROOT/"debian.txt").read_text(),"debian","fixture")
    arch=package_files((ROOT/"arch.txt").read_text(),"arch","fixture")
    assert {r["command"] for r in deb}=={"git","envcp"}
    assert [r["command"] for r in arch]==["curl"]
def test_language_collectors_use_declared_bins():
    npm=npm_metadata(json.loads((ROOT/"npm.json").read_text()))
    assert (npm[0]["package"],npm[0]["command"])==("foo-tool","foocli")
    c=json.loads((ROOT/"crates.json").read_text())
    assert crates_manifest(c["manifest"],c["package"])[0]["command"]=="rcli"
def test_homebrew_bottle_inventory_and_alias():
    rows=homebrew_metadata(json.loads((ROOT/"homebrew.json").read_text()))
    assert {r["command"] for r in rows}=={"git","git-old"}
    assert next(r for r in rows if r["command"]=="git-old")["alias_of"]=="git"


def test_collectors_are_deterministic_and_intermediate_schema_valid():
    schema=json.loads((ROOT.parent.parent/"schema/intermediate.schema.json").read_text())
    validator=Draft202012Validator(schema)
    def collect():
        return (package_files((ROOT/"debian.txt").read_text(),"debian","fixture")+
                package_files((ROOT/"arch.txt").read_text(),"arch","fixture")+
                npm_metadata(json.loads((ROOT/"npm.json").read_text()))+
                homebrew_metadata(json.loads((ROOT/"homebrew.json").read_text())))
    rows=collect()
    assert [json.dumps(row,sort_keys=True) for row in rows] == [json.dumps(row,sort_keys=True) for row in collect()]
    for row in rows:
        validator.validate(row)
