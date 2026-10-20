"""Live upstream checks are explicit: `pytest -m live` performs real downloads."""
import pytest
from global_executables.live_sources import run_all

@pytest.mark.live
def test_every_supported_upstream_serves_expected_executable_evidence():
    report = run_all()
    assert report["status"] == "success"
    assert {r["ecosystem"] for r in report["results"]} == {"debian", "ubuntu", "arch", "homebrew", "npm", "pypi", "crates"}
    assert all(r["downloaded_bytes"] > 0 and r["evidence"] for r in report["results"])
