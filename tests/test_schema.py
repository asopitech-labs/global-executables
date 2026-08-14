import json
from pathlib import Path
from referencing import Registry, Resource
from jsonschema import Draft202012Validator, FormatChecker
from global_executables.pipeline import rebuild
ROOT=Path(__file__).parents[1]
def test_generated_records_validate(tmp_path):
    rebuild(tmp_path,sorted((ROOT/"fixtures/intermediate").glob("*.jsonl")),"2026-08-14")
    provider=json.loads((ROOT/"schema/provider.schema.json").read_text()); executable=json.loads((ROOT/"schema/executable.schema.json").read_text())
    registry=Registry().with_resource(provider["$id"],Resource.from_contents(provider))
    validator=Draft202012Validator(executable,registry=registry,format_checker=FormatChecker())
    for path in (tmp_path/"data/executables").glob("**/*.json"): validator.validate(json.loads(path.read_text()))
