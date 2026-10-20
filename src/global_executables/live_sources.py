"""Bounded real-upstream probes used to validate collector assumptions.

These probes are deliberately separate from deterministic fixture tests.  They
download and inspect actual upstream content; they are smoke checks, not a full
crawl and never generate canonical data.
"""
from __future__ import annotations

import gzip
import io
import json
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

USER_AGENT = "global-executables/0.1 (+https://github.com/asopitech-labs/global-executables)"


def fetch(url: str, *, accept: str | None = None) -> tuple[bytes, dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    started = time.monotonic()
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as error:
        challenge = error.headers.get("WWW-Authenticate", "")
        if error.code != 401 or not challenge.startswith("Bearer "):
            raise
        fields = dict(part.strip().split("=", 1) for part in challenge.removeprefix("Bearer ").split(","))
        realm = fields.pop("realm").strip('"')
        query = urllib.parse.urlencode({key: value.strip('"') for key, value in fields.items()})
        token_body, _ = fetch(f"{realm}?{query}")
        token = json.loads(token_body).get("token") or json.loads(token_body)["access_token"]
        headers["Authorization"] = f"Bearer {token}"
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)
    with response:
        body = response.read()
        final_url = response.url
        status = response.status
        rate_limit = {k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit")}
    return body, {"url": url, "final_url": final_url, "status": status, "downloaded_bytes": len(body),
                  "duration_seconds": round(time.monotonic() - started, 3), "rate_limit": rate_limit}


def probe_contents(ecosystem: str, url: str, expected_path: str) -> dict[str, Any]:
    body, result = fetch(url)
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
        match = next((line.decode("utf-8", "replace").strip() for line in archive if line.split(maxsplit=1)[0] == expected_path.encode()), None)
    if not match:
        raise RuntimeError(f"{ecosystem}: {expected_path} absent from downloaded Contents index")
    result.update(ecosystem=ecosystem, evidence=match, evidence_kind="filesystem-index")
    return result


def probe_arch() -> dict[str, Any]:
    body, result = fetch("https://geo.mirror.pkgbuild.com/core/os/x86_64/core.files")
    evidence = None
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
        for member in archive:
            if member.isfile() and member.name.endswith("/files"):
                text = archive.extractfile(member).read().decode("utf-8", "replace")
                if "usr/bin/pacman\n" in text:
                    evidence = f"{member.name}:usr/bin/pacman"
                    break
    if not evidence:
        raise RuntimeError("arch: usr/bin/pacman absent from downloaded files database")
    result.update(ecosystem="arch", evidence=evidence, evidence_kind="filesystem-index")
    return result


def probe_npm() -> dict[str, Any]:
    body, result = fetch("https://registry.npmjs.org/typescript/latest")
    metadata = json.loads(body); bins = metadata.get("bin", {})
    if "tsc" not in bins:
        raise RuntimeError("npm: typescript latest metadata does not declare tsc")
    tarball, artifact = fetch(metadata["dist"]["tarball"])
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        if bins["tsc"].lstrip("./") not in {m.name.removeprefix("package/") for m in archive}:
            raise RuntimeError("npm: declared tsc target absent from package tarball")
    result.update(ecosystem="npm", evidence=f"typescript@{metadata['version']}:tsc->{bins['tsc']}", evidence_kind="declared-and-artifact",
                  artifact_downloaded_bytes=artifact["downloaded_bytes"])
    return result


def probe_pypi() -> dict[str, Any]:
    body, result = fetch("https://pypi.org/pypi/pip/json")
    metadata = json.loads(body); version = metadata["info"]["version"]
    wheel = next(file for file in metadata["urls"] if file["packagetype"] == "bdist_wheel")
    artifact, artifact_result = fetch(wheel["url"])
    evidence = None
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        entry = next(n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt"))
        text = archive.read(entry).decode()
        if any(line.split("=", 1)[0].strip() == "pip" for line in text.splitlines() if "=" in line):
            evidence = f"pip=={version}:{entry}:pip"
    if not evidence:
        raise RuntimeError("pypi: pip console script absent from downloaded wheel")
    result.update(ecosystem="pypi", evidence=evidence, evidence_kind="declared-and-artifact",
                  artifact_downloaded_bytes=artifact_result["downloaded_bytes"])
    return result


def probe_crates() -> dict[str, Any]:
    body, result = fetch("https://crates.io/api/v1/crates/ripgrep")
    metadata = json.loads(body); version = metadata["crate"]["max_stable_version"]
    artifact, artifact_result = fetch(f"https://crates.io/api/v1/crates/ripgrep/{version}/download")
    with tempfile.TemporaryDirectory() as directory:
        with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as archive:
            archive.extractall(directory, filter="data")
        manifest = next(Path(directory).glob("*/Cargo.toml"))
        cargo = subprocess.run(["cargo", "metadata", "--format-version", "1", "--no-deps", "--manifest-path", str(manifest)], check=True, capture_output=True, text=True)
        targets = json.loads(cargo.stdout)["packages"][0]["targets"]
        if not any(t["name"] == "rg" and "bin" in t["kind"] for t in targets):
            raise RuntimeError("crates: cargo metadata did not resolve ripgrep's rg binary target")
    result.update(ecosystem="crates", evidence=f"ripgrep@{version}:rg", evidence_kind="cargo-metadata-and-artifact",
                  artifact_downloaded_bytes=artifact_result["downloaded_bytes"])
    return result


def probe_go() -> dict[str, Any]:
    versions_body, result = fetch("https://proxy.golang.org/golang.org/x/tools/@v/list")
    versions = [line.strip() for line in versions_body.decode("utf-8", "replace").splitlines() if line.strip()]
    version = versions[-1]
    artifact, artifact_result = fetch(f"https://proxy.golang.org/golang.org/x/tools/@v/{urllib.parse.quote(version, safe='')}.zip")
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        candidates = [name for name in archive.namelist()
                      if f"golang.org/x/tools@{version}/cmd/goimports/" in name
                      and name.endswith(".go") and not name.endswith("_test.go")]
        evidence_file = next((name for name in candidates if b"package main" in archive.read(name)), None)
        if evidence_file is None:
            raise RuntimeError("go: x/tools goimports main package absent from module archive")
    result.update(ecosystem="go", evidence=f"golang.org/x/tools@{version}:goimports:{evidence_file}", evidence_kind="declared-and-artifact",
                  artifact_downloaded_bytes=artifact_result["downloaded_bytes"])
    return result


def probe_homebrew() -> dict[str, Any]:
    body, result = fetch("https://formulae.brew.sh/api/formula/jq.json")
    metadata = json.loads(body); stable = metadata["versions"]["stable"]
    bottle = metadata["bottle"]["stable"]["files"]
    selected = next(iter(bottle.values()))
    artifact, artifact_result = fetch(selected["url"], accept="application/vnd.oci.image.layer.v1.tar+gzip")
    evidence = None
    with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:*") as archive:
        evidence = next((m.name for m in archive if m.name.endswith("/bin/jq")), None)
    if not evidence:
        raise RuntimeError("homebrew: bin/jq absent from downloaded bottle")
    result.update(ecosystem="homebrew", evidence=f"jq@{stable}:{evidence}", evidence_kind="bottle-filesystem",
                  artifact_downloaded_bytes=artifact_result["downloaded_bytes"])
    return result


def run_all() -> dict[str, Any]:
    probes = [
        lambda: probe_contents("debian", "https://deb.debian.org/debian/dists/stable/main/Contents-amd64.gz", "usr/bin/git"),
        lambda: probe_contents("ubuntu", "https://archive.ubuntu.com/ubuntu/dists/noble/Contents-amd64.gz", "usr/bin/git"),
        probe_arch, probe_homebrew, probe_npm, probe_pypi, probe_crates, probe_go,
    ]
    results = [probe() for probe in probes]
    return {"status": "success", "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results,
            "total_downloaded_bytes": sum(r["downloaded_bytes"] + r.get("artifact_downloaded_bytes", 0) for r in results)}
