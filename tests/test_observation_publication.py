"""Observation publication must keep the replacement a recipe snapshot made (#58).

The cpp-registries run of 2026-10-08 (artifact-data 7787cd0) parsed every vcpkg port
and xmake package with the #60 collector, which dropped rows such as mnn `cpp` and
openexr `not`.  `crawl_parallel.sh publish` then merged the collector output into
the published copy by row identity and re-added every dropped row.  These tests run
that publication path against a trimmed copy of the rows 7787cd0 published.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures/collectors"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import merge_observations  # noqa: E402
from global_executables import production  # noqa: E402

ROWS = "data/production/intermediate"
TOOLS = ("tools/crawl_parallel.sh", "tools/merge_observations.py", "tools/merge_registry_publication.py",
         "tools/registry_state.py", "tools/transport_shards.py", "src/global_executables/__init__.py",
         "src/global_executables/registry_state.py")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def tarball(members: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, BytesIO(payload))
    return stream.getvalue()


def snapshots() -> dict[str, bytes]:
    """Upstream snapshots holding the recipes the published sample came from."""
    binary = b'    set_kind("binary")\n    add_versions("1.0", "aa")\n'
    return {
        "vcpkg": tarball({
            "vcpkg-master/ports/mnn/portfile.cmake": (FIXTURES / "vcpkg-mnn-portfile.cmake").read_bytes(),
            "vcpkg-master/ports/mnn/vcpkg.json": b'{"name": "mnn", "version": "1.1.0"}',
            "vcpkg-master/ports/openexr/portfile.cmake": (FIXTURES / "vcpkg-openexr-portfile.cmake").read_bytes(),
            "vcpkg-master/ports/openexr/vcpkg.json": b'{"name": "openexr", "version": "3.5.2"}',
        }),
        "xmake": tarball({
            "xmake-repo-master/packages/a/autotools/xmake.lua": b'package("autotools")\n' + binary,
            "xmake-repo-master/packages/b/binutils/xmake.lua": b'package("binutils")\n' + binary,
            "xmake-repo-master/packages/m/meson/xmake.lua": (FIXTURES / "xmake-meson.lua").read_bytes(),
        }),
    }


def serve(bodies: dict[str, bytes], url: str) -> tuple[bytes, dict]:
    body = bodies["xmake" if "xmake" in url else "vcpkg"]
    return body, {"downloaded_bytes": len(body)}


def published_rows(origin: Path, source: str) -> list[dict]:
    text = git(origin, "show", f"artifact-data:{ROWS}/{source}.jsonl")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def commands(rows: list[dict], package: str) -> list[str]:
    return sorted(row["command"] for row in rows if row["package"] == package)


def test_publication_keeps_the_rows_a_recipe_snapshot_replaced(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp_path / "seed"
    (seed / ROWS).mkdir(parents=True)
    git(seed, "init", "-q", "-b", "artifact-data")
    for source in ("vcpkg", "xmake"):
        shutil.copy2(FIXTURES / f"artifact-data-7787cd0-{source}.jsonl", seed / ROWS / f"{source}.jsonl")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "7787cd0 sample")
    git(seed, "push", "-q", f"file://{origin}", "artifact-data")
    before = {source: published_rows(origin, source) for source in ("vcpkg", "xmake")}
    assert {"cpp", "converter", "train"} <= set(commands(before["vcpkg"], "mnn"))
    assert {"not", "exrcheck"} <= set(commands(before["vcpkg"], "openexr"))
    assert commands(before["xmake"], "autotools") == ["autotools"]

    work = tmp_path / "work"
    for relative in TOOLS:
        (work / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, work / relative)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "tools")
    git(work, "remote", "add", "origin", f"file://{origin}")

    # The recipes job restores the published rows, then collects over them.
    intermediate = work / ROWS
    intermediate.mkdir(parents=True)
    for source in ("vcpkg", "xmake"):
        shutil.copy2(FIXTURES / f"artifact-data-7787cd0-{source}.jsonl", intermediate / f"{source}.jsonl")
    bodies = snapshots()
    monkeypatch.setattr(production, "fetch",
                        lambda url, timeout=300: serve(bodies, url))
    production.crawl_sources(["vcpkg", "xmake"], intermediate, work / "reports/cpp-registry-crawl.json")

    environment = {**os.environ, "BASE": str(tmp_path / "ci-registry"), "SOURCES": "",
                   "OBSERVATION_SOURCES": "vcpkg xmake", "OBSERVATION_REPORT": "reports/cpp-registry-crawl.json",
                   "CONTAINER_RUNTIME": "true", "PUBLISH_LOCK": str(tmp_path / "publish.lock"),
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    completed = subprocess.run(["bash", str(work / "tools/crawl_parallel.sh"), "publish"], cwd=work,
                               env=environment, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "published" in completed.stdout.splitlines()

    vcpkg = published_rows(origin, "vcpkg")
    mnn = commands(vcpkg, "mnn")
    for stale in ("cpp", "converter", "evaluation", "quantization", "train",
                  "benchmark.out", "benchmarkExprModels.out", "run_test.out"):
        assert stale not in mnn
    assert {"MNNConvert", "MNNDump2Json", "train.out"} <= set(mnn)
    openexr = commands(vcpkg, "openexr")
    assert "not" not in openexr and "exrcheck" not in openexr
    assert {"exr2aces", "exrinfo", "exrheader"} <= set(openexr)
    # A port absent from this snapshot keeps its durable evidence.
    assert commands(vcpkg, "cppcms") == commands(before["vcpkg"], "cppcms") != []

    xmake = published_rows(origin, "xmake")
    assert commands(xmake, "autotools") == [] and commands(xmake, "binutils") == []
    assert [row["version"] for row in xmake if row["package"] == "meson"] == ["1.12.1"]
    assert commands(xmake, "7z") == ["7z"]

    # The merge bookkeeping stays on the runner.
    tree = git(origin, "ls-tree", "-r", "--name-only", "artifact-data").splitlines()
    assert not [path for path in tree if path.endswith(".reparsed.json")]


def test_merge_without_a_reparse_still_accumulates():
    published = [{"command": "old", "ecosystem": "vcpkg", "package": "p", "source": "s", "version": "1"},
                 {"command": "keep", "ecosystem": "vcpkg", "package": "q", "source": "s"}]
    observed = [{"command": "old", "ecosystem": "vcpkg", "package": "p", "source": "s", "version": "2"}]
    rows = merge_observations.merge(observed, published, set())
    assert rows == [{"command": "keep", "ecosystem": "vcpkg", "package": "q", "source": "s"}, observed[0]]

    rows = merge_observations.merge([], published, {("vcpkg", "p")})
    assert [row["command"] for row in rows] == ["keep"]


def test_merge_reproduces_the_published_order(tmp_path):
    published = (FIXTURES / "artifact-data-7787cd0-vcpkg.jsonl").read_text()
    target = tmp_path / "vcpkg.jsonl"
    target.write_text(published)
    merge_observations.main(["--observed", str(tmp_path / "absent.jsonl"), "--target", str(target)])
    # Without new observations the published bytes are unchanged, so no spurious commit.
    assert target.read_text() == published


def test_collector_sidecar_follows_the_run(tmp_path, monkeypatch):
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    bodies = snapshots()
    monkeypatch.setattr(production, "fetch",
                        lambda url, timeout=300: serve(bodies, url))
    production.crawl_sources(["vcpkg"], intermediate, tmp_path / "report.json")
    sidecar = production.reparsed_path(intermediate / "vcpkg.jsonl")
    assert json.loads(sidecar.read_text()) == {"packages": [["vcpkg", "mnn"], ["vcpkg", "openexr"]]}

    # A later run that cannot read the snapshot must not leave the old package set behind.
    def unavailable(url, timeout=300):
        raise production.ProductionSourceError("offline")
    monkeypatch.setattr(production, "fetch", unavailable)
    try:
        production.crawl_sources(["vcpkg"], intermediate, tmp_path / "report.json")
    except Exception:
        pass
    assert not sidecar.exists()
