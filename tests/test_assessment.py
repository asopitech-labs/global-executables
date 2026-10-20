from pathlib import Path

from global_executables.assessment import assess
from global_executables.pipeline import rebuild
from global_executables.search import Dataset

ROOT = Path(__file__).parents[1]
INPUTS = sorted((ROOT / "fixtures/intermediate").glob("*.jsonl"))


def test_assessment_separates_existence_from_activity(tmp_path):
    rebuild(tmp_path, INPUTS, "2026-08-14")
    active = assess(Dataset(tmp_path), "git")
    missing = assess(Dataset(tmp_path), "new-cli")
    assert active["found"] is True
    assert active["assessment"]["collision_risk"] == "active_common"
    assert missing["found"] is False
    assert missing["assessment"]["collision_risk"] == "insufficient_evidence"


def test_assessment_scope_is_composable(tmp_path):
    rebuild(tmp_path, INPUTS, "2026-08-14")
    result = assess(Dataset(tmp_path), "envcp", {"language": "javascript", "registry": "npm"})
    assert result["found"] is True
    assert result["scope"] == {"language": "javascript", "registry": "npm"}
