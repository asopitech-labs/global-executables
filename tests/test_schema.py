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
    metadata=json.loads((ROOT/"schema/metadata.schema.json").read_text())
    Draft202012Validator(metadata,format_checker=FormatChecker()).validate(json.loads((tmp_path/"data/metadata.json").read_text()))


def test_freshness_manifest_and_report_validate(tmp_path):
    from datetime import datetime, timezone
    from global_executables.freshness import run_scan

    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({"command": "fresh", "ecosystem": "fixture", "package": "fresh",
                                  "version": "1", "repository": None, "source": "fixture",
                                  "confidence": "direct"}) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "partitions": [
        {"id": "fixture/all", "source": "fixture", "input": "source.jsonl"}
    ]}))
    report_path = tmp_path / "reports/freshness.json"
    run_scan(tmp_path, manifest, tmp_path / "data/freshness/state.json", report_path,
             observed_at=datetime(2026, 8, 15, tzinfo=timezone.utc))
    manifest_schema = json.loads((ROOT/"schema/freshness-manifest.schema.json").read_text())
    report_schema = json.loads((ROOT/"schema/freshness-report.schema.json").read_text())
    Draft202012Validator(manifest_schema, format_checker=FormatChecker()).validate(json.loads(manifest.read_text()))
    Draft202012Validator(report_schema, format_checker=FormatChecker()).validate(json.loads(report_path.read_text()))
