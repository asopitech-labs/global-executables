"""The sharded registry state layout: format, migration, atomicity and publication.

internal/gocrawl/testdata/registry-state/legacy-registry-state.json.gz is a trimmed
copy of the real artifact-data ``registry-state.json`` (go and pypi ``unavailable``
maps cut to 8,500 and 1,100 entries, the small sources whole) plus an ``edge-cases``
source with the strings and numbers on which JSON encoders disagree.  manifest.golden.json pins the
SHA-256 of every file the layout produces; internal/gocrawl/statestore_test.go checks
the Go writer against the same manifest, so both languages write identical bytes.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from global_executables import registry_state
from global_executables.registry_state import (
    MANIFEST,
    STAGED_FILES,
    STAGING,
    RegistryStateError,
    encode_layout,
    encode_pretty,
    load_state,
    prefix_length,
    recover,
    save_state,
    shard_name,
    state_exists,
    state_paths,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "internal/gocrawl/testdata/registry-state"
CLI = ROOT / "tools/registry_state.py"


def legacy_fixture(directory: Path) -> Path:
    path = directory / "registry-state.json"
    path.write_bytes(gzip.decompress((FIXTURES / "legacy-registry-state.json.gz").read_bytes()))
    return path


def layout_files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_paths_accept_the_legacy_file_or_the_directory():
    for argument in ("data/production/registry-state.json", "data/production/registry-state"):
        paths = state_paths(argument)
        assert paths.root == Path("data/production/registry-state")
        assert paths.legacy == Path("data/production/registry-state.json")


def test_legacy_file_migrates_to_the_golden_layout(tmp_path):
    legacy = legacy_fixture(tmp_path)
    original = json.loads(legacy.read_text())

    assert load_state(legacy) == original  # legacy-read fallback
    result = save_state(legacy, load_state(legacy))

    root = tmp_path / "registry-state"
    assert result.legacy_removed and not legacy.exists()
    assert (root / MANIFEST).read_bytes() == (FIXTURES / "manifest.golden.json").read_bytes()
    assert load_state(root) == load_state(legacy) == original
    files = layout_files(root)
    assert len(files) == 28 and not any(name.startswith(STAGING) for name in files)
    assert {"go/unavailable/0-0.jsonl", "go/unavailable/f-f.jsonl", "pypi/unavailable/all.jsonl",
            "edge-cases/unavailable/all.jsonl", "conan/source.json"} <= set(files)
    manifest = json.loads(files[MANIFEST])
    assert manifest["sources"]["go"]["fields"] == {"unavailable": {"entries": 8500, "prefix_length": 1}}
    assert manifest["sources"]["go"]["summary"]["cursor"] == original["sources"]["go"]["cursor"]
    assert "unavailable" not in json.loads(files["go/source.json"])


def git_pack_name_hash(path: str) -> int:
    """Git's pack_name_hash: pairs a changed blob with its previous version for deltas."""
    value = 0
    for byte in path.encode():
        if chr(byte).isspace():
            continue
        value = ((value >> 2) + (byte << 24)) & 0xFFFFFFFF
    return value


@pytest.mark.parametrize("length", [1, 2, 3])
def test_shard_names_stay_distinct_to_gits_delta_pairing(length):
    names = [f"data/production/registry-state/go/unavailable/{registry_state.shard_file_name(f'{index:0{length}x}')}"
             for index in range(16**length)]
    assert len({git_pack_name_hash(name) for name in names}) == len(names)


def test_manifest_matches_its_schema():
    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "schema/registry-state-manifest.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads((FIXTURES / "manifest.golden.json").read_text()))


def test_shards_are_sorted_hash_partitioned_and_deterministic(tmp_path):
    document = load_state(legacy_fixture(tmp_path))
    first, second = tmp_path / "first", tmp_path / "second"
    save_state(first, document)
    registry_state._SHARD_CACHE.clear()
    save_state(second, load_state(first))
    assert layout_files(first) == layout_files(second)

    for name, body in layout_files(first).items():
        if not name.startswith("go/unavailable/"):
            continue
        keys = [json.loads(line)[0] for line in body.decode().splitlines()]
        assert keys == sorted(keys)
        assert {shard_name(key, 1) for key in keys} == {name.rsplit("/", 1)[1]}

    assert [prefix_length(count) for count in (1, 8192, 8193, 131072, 131073, 2_097_153)] == [0, 0, 1, 1, 2, 3]


def test_a_change_rewrites_only_its_shard_and_the_manifest(tmp_path):
    root = tmp_path / "registry-state"
    document = load_state(legacy_fixture(tmp_path))
    save_state(root, document)
    before = layout_files(root)
    inodes = {name: (root / name).stat().st_ino for name in before}

    document["sources"]["go"]["unavailable"]["zz.example/added"] = "HTTP 404: Not Found"
    result = save_state(root, document)

    after = layout_files(root)
    changed = sorted(name for name in after if before.get(name) != after[name])
    assert changed == sorted([f"go/unavailable/{shard_name('zz.example/added', 1)}", MANIFEST])
    assert result.written == 1
    for name in set(before) - set(changed):
        assert (root / name).stat().st_ino == inodes[name]

    unchanged = save_state(root, document)
    assert unchanged.written == 0 and layout_files(root) == after


def test_shrinking_a_map_inlines_it_and_removes_its_shards(tmp_path):
    root = tmp_path / "registry-state"
    document = {"version": 1, "sources": {"pypi": {"cursor": 1, "unavailable": {f"p{i}": "gone" for i in range(2000)}}}}
    save_state(root, document)
    assert (root / "pypi/unavailable/all.jsonl").is_file()
    document["sources"]["pypi"]["unavailable"] = {"p1": "gone"}
    save_state(root, document)
    assert not (root / "pypi/unavailable").exists()
    assert load_state(root) == document
    del document["sources"]["pypi"]
    save_state(root, document)
    assert not (root / "pypi").exists() and load_state(root) == document


def test_uncommitted_staging_is_ignored_and_discarded(tmp_path):
    root = tmp_path / "registry-state"
    save_state(root, {"version": 1, "sources": {"npm": {"cursor": 1}}})
    staged = root / STAGING / STAGED_FILES / "npm/source.json"
    staged.parent.mkdir(parents=True)
    staged.write_text('{"cursor": 9')  # a writer died before its manifest commit

    assert load_state(root)["sources"]["npm"]["cursor"] == 1
    recover(root)
    assert not (root / STAGING).exists()
    assert load_state(root)["sources"]["npm"]["cursor"] == 1


def test_a_writer_interrupted_after_commit_rolls_forward(tmp_path, monkeypatch):
    root = tmp_path / "registry-state"
    old = {"version": 1, "sources": {"npm": {"cursor": 1}, "go": {"cursor": 5, "unavailable": {f"m{i}": "x" for i in range(1500)}}}}
    new = json.loads(json.dumps(old))
    new["sources"]["npm"]["cursor"] = 2
    new["sources"]["go"]["unavailable"]["m-new"] = "y"
    save_state(root, old)

    real_replace = os.replace
    calls = {"count": 0}

    def crash_after_first_rename(source, target):
        if f"/{STAGING}/{STAGED_FILES}/" in str(source) and f"/{STAGING}/" not in str(target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated crash while applying the staged files")
        return real_replace(source, target)

    monkeypatch.setattr(registry_state.os, "replace", crash_after_first_rename)
    with pytest.raises(OSError, match="simulated crash"):
        save_state(root, new)
    monkeypatch.setattr(registry_state.os, "replace", real_replace)

    # Half applied: readers already see the committed document, never a mixture.
    assert (root / STAGING / MANIFEST).is_file()
    assert load_state(root) == new
    recover(root)
    assert not (root / STAGING).exists()
    assert load_state(root) == new


def test_a_writer_interrupted_before_commit_keeps_the_old_state(tmp_path, monkeypatch):
    root = tmp_path / "registry-state"
    old = {"version": 1, "sources": {"npm": {"cursor": 1}}}
    save_state(root, old)
    real_write = registry_state._write_file

    def crash_on_manifest(path, data):
        if path.name == MANIFEST:
            raise OSError("simulated crash before the commit point")
        real_write(path, data)

    monkeypatch.setattr(registry_state, "_write_file", crash_on_manifest)
    with pytest.raises(OSError):
        save_state(root, {"version": 1, "sources": {"npm": {"cursor": 2}}})
    monkeypatch.setattr(registry_state, "_write_file", real_write)

    assert load_state(root) == old
    save_state(root, {"version": 1, "sources": {"npm": {"cursor": 3}}})
    assert load_state(root)["sources"]["npm"]["cursor"] == 3 and not (root / STAGING).exists()


def test_a_file_that_does_not_match_the_manifest_is_never_returned(tmp_path, monkeypatch):
    root = tmp_path / "registry-state"
    save_state(root, {"version": 1, "sources": {"npm": {"cursor": 1}}})
    (root / "npm/source.json").write_text('{\n  "cursor": 5\n}\n')
    monkeypatch.setattr(registry_state.time, "sleep", lambda _seconds: None)
    with pytest.raises(RegistryStateError, match="kept changing"):
        load_state(root)


def test_missing_state_returns_the_default_and_rejects_unsafe_names(tmp_path):
    assert load_state(tmp_path / "missing") == {"version": 1, "sources": {}}
    assert load_state(tmp_path / "missing", {"sources": {}}) == {"sources": {}}
    assert not state_exists(tmp_path / "missing")
    with pytest.raises(RegistryStateError):
        save_state(tmp_path / "bad", {"version": 1, "sources": {"../escape": {}}})
    manifest, _files = encode_layout({"version": 1, "sources": {}})
    manifest["files"]["../escape"] = {"bytes": 0, "sha256": ""}
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / MANIFEST).write_bytes(encode_pretty(manifest))
    with pytest.raises(RegistryStateError, match="unsafe path"):
        load_state(tmp_path / "evil")


def git(directory: Path, *args: str) -> str:
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                          text=True, env=environment).stdout


def cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CLI), *args], check=check, capture_output=True, text=True)


def test_cli_restores_either_published_layout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "data/production").mkdir(parents=True)
    legacy = {"version": 1, "sources": {"crates": {"cursor": 3}, "go": {"cursor": 4}}}
    (repo / "data/production/registry-state.json").write_text(json.dumps(legacy))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "legacy")
    target = tmp_path / "restored/data/production/registry-state"

    assert "legacy" in cli("restore", "--repo", str(repo), "--ref", "HEAD", "--state", str(target)).stdout
    assert load_state(target) == legacy and (tmp_path / "restored/data/production/registry-state.json").is_file()

    save_state(repo / "data/production/registry-state", legacy | {"sources": {"crates": {"cursor": 7}}})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "migrate")
    assert "sharded" in cli("restore", "--repo", str(repo), "--ref", "HEAD", "--state", str(target)).stdout
    assert load_state(target)["sources"] == {"crates": {"cursor": 7}}
    assert not (tmp_path / "restored/data/production/registry-state.json").exists()

    missing = cli("restore", "--repo", str(repo), "--ref", "HEAD", "--state", str(tmp_path / "x"),
                  "--published-path", "nowhere", "--require", check=False)
    assert missing.returncode == 1 and "missing" in missing.stdout


def test_cli_copy_merge_get_and_summary(tmp_path):
    base = tmp_path / "base"
    save_state(base, {"version": 1, "sources": {"crates": {"cursor": 1, "failures": {}}, "go": {"cursor": 2}}})
    cli("copy", "--from", str(base), "--to", str(tmp_path / "slice"), "--only-source", "go")
    assert load_state(tmp_path / "slice")["sources"] == {"go": {"cursor": 2}}

    save_state(tmp_path / "slice", {"version": 1, "sources": {"go": {"cursor": 9}}})
    cli("merge-sources", "--into", str(base), "--from", str(tmp_path / "slice"), "--from", str(tmp_path / "absent"))
    assert load_state(base)["sources"]["go"] == {"cursor": 9}

    assert json.loads(cli("get", "--state", str(base), "--source", "crates", "--scalars").stdout) == {"cursor": 1}
    assert json.loads(cli("get", "--state", str(base), "--source", "nope").stdout) is None
    summary = json.loads(cli("summary", "--state", str(base)).stdout)
    assert summary["layout"] == "sharded" and summary["sources"]["crates"]["sizes"] == {"failures": 0}
    assert cli("exists", "--state", str(base)).returncode == 0
    assert cli("exists", "--state", str(tmp_path / "absent"), check=False).returncode == 1


def test_publication_migrates_the_branch_in_its_normal_commit(tmp_path):
    """Run crawl_parallel.sh publish against a local origin holding the legacy file."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "artifact-data")
    (seed / "data/production").mkdir(parents=True)
    legacy = legacy_fixture(seed / "data/production")
    published = json.loads(legacy.read_text())
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "legacy state")
    git(seed, "push", "-q", f"file://{origin}", "artifact-data")

    work = tmp_path / "work"
    for relative in ("tools/crawl_parallel.sh", "tools/merge_registry_publication.py", "tools/registry_state.py",
                     "tools/transport_shards.py", "src/global_executables/__init__.py",
                     "src/global_executables/registry_state.py"):
        (work / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, work / relative)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "tools")
    git(work, "remote", "add", "origin", f"file://{origin}")

    base = tmp_path / "ci-registry"
    local = json.loads(json.dumps(published))
    local["sources"]["nuget"]["cursor"] = published["sources"]["nuget"]["cursor"] + 10
    local["sources"] = {"nuget": local["sources"]["nuget"]}
    save_state(Path(f"{base}-nuget") / "data/production/registry-state", local)

    environment = {**os.environ, "BASE": str(base), "SOURCES": "nuget", "OBSERVATION_SOURCES": " ",
                   "CONTAINER_RUNTIME": "true", "PUBLISH_LOCK": str(tmp_path / "publish.lock"),
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

    def publish() -> str:
        completed = subprocess.run(["bash", str(work / "tools/crawl_parallel.sh"), "publish"], cwd=work,
                                   env=environment, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    assert "published" in publish().splitlines()
    changes = git(origin, "show", "--name-status", "--format=", "artifact-data").splitlines()
    assert "D\tdata/production/registry-state.json" in changes
    assert "A\tdata/production/registry-state/manifest.json" in changes
    assert "A\tdata/production/registry-state/go/unavailable/0-0.jsonl" in changes

    restored = tmp_path / "restored/registry-state"
    git(work, "fetch", "-q", "origin", "artifact-data")
    cli("restore", "--repo", str(work), "--ref", "origin/artifact-data", "--state", str(restored))
    expected = json.loads(json.dumps(published))
    expected["sources"]["nuget"] = local["sources"]["nuget"]
    assert load_state(restored) == expected

    # The next run changes one checkpoint: the commit touches its file and the manifest.
    local["sources"]["nuget"]["cursor"] += 5
    save_state(Path(f"{base}-nuget") / "data/production/registry-state", local)
    assert "published" in publish().splitlines()
    changes = git(origin, "show", "--name-status", "--format=", "artifact-data").splitlines()
    assert sorted(changes) == ["M\tdata/production/registry-state/manifest.json",
                               "M\tdata/production/registry-state/nuget/source.json"]


def test_change_driven_fields_are_stored_canonically_and_match_the_go_golden(tmp_path):
    """`checked`, `feed_cursor` and `feed_pending` ride the generic map sharding.

    The golden directory is read and rewritten byte for byte by
    internal/gocrawl (TestStateGoldenWithChecksIsByteIdentical): both encoders agree.
    """
    golden = Path(__file__).resolve().parents[1] / "fixtures" / "refresh" / "state-golden"
    document = load_state(golden)
    pypi = document["sources"]["pypi"]
    assert len(pypi["checked"]) == 1500 and len(pypi["feed_pending"]) == 1200 and pypi["feed_cursor"] == "42006261"
    save_state(tmp_path / "again", document)
    original = {path.relative_to(golden): path.read_bytes() for path in golden.rglob("*") if path.is_file()}
    rewritten = {path.relative_to(tmp_path / "again"): path.read_bytes()
                 for path in (tmp_path / "again").rglob("*") if path.is_file()}
    assert rewritten == original
