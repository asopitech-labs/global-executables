import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
TOOL = ROOT / "tools/transport_shards.py"
PARALLEL = (ROOT / "tools/crawl_parallel.sh").read_text()
REFRESH = (ROOT / ".github/workflows/refresh.yml").read_text()


def run_tool(*args: object, expected: int = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, TOOL, *(str(arg) for arg in args)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == expected, completed.stderr
    return completed


def test_pack_is_bounded_deterministic_and_round_trips(tmp_path):
    source = tmp_path / "go.jsonl"
    rows = [json.dumps({"command": f"tool-{index}", "package": "example"}) + "\n" for index in range(20)]
    source.write_text("".join(rows))
    first = tmp_path / "first"
    second = tmp_path / "second"

    run_tool("pack", "--input", source, "--output-dir", first, "--max-uncompressed-bytes", 128)
    run_tool("pack", "--input", source, "--output-dir", second, "--max-uncompressed-bytes", 128)

    manifest = json.loads((first / "manifest.json").read_text())
    assert manifest["format"] == "global-executables-gzip-shards-v1"
    assert len(manifest["parts"]) > 1
    assert all(part["uncompressed_bytes"] <= 128 for part in manifest["parts"])
    assert {
        path.name: path.read_bytes() for path in first.iterdir()
    } == {
        path.name: path.read_bytes() for path in second.iterdir()
    }

    restored = tmp_path / "restored.jsonl"
    run_tool("unpack", "--input-dir", first, "--output", restored)
    assert restored.read_bytes() == source.read_bytes()


def test_unpack_corruption_fails_without_replacing_existing_output(tmp_path):
    source = tmp_path / "go.jsonl"
    source.write_text('{"command":"go-tool"}\n')
    transport = tmp_path / "transport"
    run_tool("pack", "--input", source, "--output-dir", transport)
    output = tmp_path / "output.jsonl"
    output.write_text("preserve me\n")

    part = next(transport.glob("part-*.jsonl.gz"))
    part.write_bytes(part.read_bytes()[:-1] + b"x")

    completed = run_tool("unpack", "--input-dir", transport, "--output", output, expected=1)
    assert "digest mismatch" in completed.stderr
    assert output.read_text() == "preserve me\n"


def test_unpack_missing_part_fails_closed(tmp_path):
    source = tmp_path / "go.jsonl"
    source.write_text('{"command":"go-tool"}\n')
    transport = tmp_path / "transport"
    run_tool("pack", "--input", source, "--output-dir", transport)
    next(transport.glob("part-*.jsonl.gz")).unlink()

    completed = run_tool(
        "unpack", "--input-dir", transport, "--output", tmp_path / "output.jsonl", expected=1
    )
    assert "part set does not match manifest" in completed.stderr
    assert not (tmp_path / "output.jsonl").exists()


def test_go_publisher_uses_shards_and_removes_legacy_transport_files():
    assert "tools/transport_shards.py\" pack" in PARALLEL
    assert "transport/go-observations" in PARALLEL
    assert "transport/go-modules" in PARALLEL
    assert 'rm -f "${worktree}/data/production/intermediate/go.jsonl"' in PARALLEL
    assert 'rm -f "${worktree}/data/production/go-modules.txt"' in PARALLEL


def test_seed_and_refresh_prefer_verified_shards_with_legacy_fallback():
    assert "tools/transport_shards.py\" unpack" in PARALLEL
    assert "origin/artifact-data:${rows}" in PARALLEL
    assert "tools/transport_shards.py unpack" in REFRESH
    assert "origin/artifact-data:data/production/intermediate/$source.jsonl" in REFRESH
