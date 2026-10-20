"""Pure parsers used by local collector CLIs and CI orchestration."""
from __future__ import annotations
import json, re, tarfile, zipfile
from pathlib import Path

EXEC_DIRS = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/", "/usr/local/sbin/")

def record(command, ecosystem, package, version=None, repository=None, source="fixture", confidence="direct", alias_of=None, **attributes):
    r={"command":command,"ecosystem":ecosystem,"package":package,"version":version,"repository":repository,"source":source,"confidence":confidence}
    if alias_of: r["alias_of"]=alias_of
    r.update({key: value for key, value in attributes.items() if value is not None})
    return r

def package_files(text: str, ecosystem: str, source: str):
    """Parse Debian Contents (`path package`) or Arch `%NAME%/%FILES%` fixture data."""
    out=[]
    if "%NAME%" in text:
        blocks=text.split("\n\n")
        for block in blocks:
            lines=block.splitlines(); pkg=lines[1] if len(lines)>1 and lines[0]=="%NAME%" else None
            for p in lines[lines.index("%FILES%")+1:] if pkg and "%FILES%" in lines else []:
                full="/"+p.lstrip("/")
                if any(full.startswith(d) for d in EXEC_DIRS) and not full.endswith("/"):
                    out.append(record(Path(full).name,ecosystem,pkg,source=source,confidence="filesystem",
                                      source_type="os_package", package_system="pacman",
                                      distribution_family="arch", distribution="archlinux"))
    else:
        for line in text.splitlines():
            parts=line.rsplit(maxsplit=1)
            if len(parts)!=2: continue
            path,pkg=parts; full="/"+path.lstrip("/")
            if any(full.startswith(d) for d in EXEC_DIRS) and "/" not in full.rstrip("/").split("/")[-1]:
                out.append(record(Path(full).name,ecosystem,pkg.split(",")[0].split("/")[-1],source=source,confidence="filesystem",
                                  source_type="os_package", package_system="deb",
                                  distribution_family="debian", distribution=ecosystem))
    return out

def npm_metadata(value, source="npm"):
    packages=value if isinstance(value,list) else [value]; out=[]
    for p in packages:
        bins=p.get("bin",{})
        if isinstance(bins,str): bins={p["name"].split("/")[-1]:bins}
        latest_version = p.get("version")
        times = p.get("time", {})
        latest_release_at = times.get(latest_version) if isinstance(times, dict) else None
        usage = p.get("downloads") or p.get("download_stats")
        for command in bins:
            out.append(record(command,"npm",p["name"],latest_version,
                              p.get("repository",{}).get("url") if isinstance(p.get("repository"),dict) else p.get("repository"),
                              source, source_type="language_package", language="javascript", registry="npm",
                              latest_release_at=latest_release_at, latest_version=latest_version,
                              usage_metrics=usage if isinstance(usage, list) else None))
    return out

def pypi_wheel(path: Path, package: str, version=None, repository=None, *, latest_release_at=None, usage_metrics=None):
    out=[]
    with zipfile.ZipFile(path) as archive:
        names=[n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
        for name in names:
            section=None
            for raw in archive.read(name).decode(errors="replace").splitlines():
                line=raw.strip()
                if line.startswith("["): section=line
                elif section=="[console_scripts]" and "=" in line:
                    out.append(record(line.split("=",1)[0].strip(),"pypi",package,version,repository,str(path),
                                      source_type="language_package", language="python", registry="pypi",
                                      latest_release_at=latest_release_at, latest_version=version,
                                      usage_metrics=usage_metrics))
    return out

def crates_manifest(text, package, version=None, repository=None, source="Cargo.toml"):
    names=re.findall(r'(?ms)^\[\[bin\]\].*?^name\s*=\s*["\']([^"\']+)',text)
    # Cargo's default binary is package name only when a conventional src/main.rs is known;
    # callers must supply explicit manifests, so absent [[bin]] is intentionally not inferred.
    return [record(n,"crates",package,version,repository,source,
                   source_type="language_package", language="rust", registry="crates.io",
                   latest_version=version) for n in names]

def homebrew_metadata(value, source="homebrew-api"):
    values=value.get("formulae",value) if isinstance(value,dict) else value; out=[]
    for formula in values:
        package=formula["name"]; version=(formula.get("versions") or {}).get("stable")
        # API analytics do not enumerate keg files. `executables` is generated by the
        # bottle inspection stage; aliases preserve explicit symlink provenance.
        for command in formula.get("executables",[]): out.append(record(command,"homebrew",package,version,formula.get("homepage"),source,"filesystem",
            source_type="os_package", package_system="homebrew", distribution_family="macos", distribution="macos"))
        for alias,target in formula.get("aliases",{}).items(): out.append(record(alias,"homebrew",package,version,formula.get("homepage"),source,"filesystem",target,
            source_type="os_package", package_system="homebrew", distribution_family="macos", distribution="macos"))
    return out
