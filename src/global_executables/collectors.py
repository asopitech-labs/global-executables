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

def package_files(text: str, ecosystem: str, source: str, *, family=None, distribution=None):
    """Parse Debian Contents (`path package`) or Arch `%NAME%/%FILES%` fixture data.

    `family`/`distribution` let a caller reuse the pacman format for a platform that
    is not Arch — MSYS2 ships Windows binaries through the same database layout.
    """
    out=[]
    if "%NAME%" in text:
        blocks=text.split("\n\n")
        for block in blocks:
            lines=block.splitlines(); pkg=lines[1] if len(lines)>1 and lines[0]=="%NAME%" else None
            for p in lines[lines.index("%FILES%")+1:] if pkg and "%FILES%" in lines else []:
                full="/"+p.lstrip("/")
                if any(full.startswith(d) for d in EXEC_DIRS) and not full.endswith("/"):
                    command=Path(full).name
                    if family == "windows": command=windows_command(command)
                    out.append(record(command,ecosystem,pkg,source=source,confidence="filesystem",
                                      source_type="os_package", package_system="pacman",
                                      distribution_family=family or "arch",
                                      distribution=distribution or "archlinux"))
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

COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@-]*\Z")
# What a Windows user types omits the extension the filesystem carries, and the whole
# point of a cross-ecosystem index is that `curl.exe` collides with `curl`.
WINDOWS_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1")

def declared_command(value):
    """Reduce a declared entry to the command an install would actually create.

    A manifest may name its executable by path — RubyGems has `../bin/code-labs`, npm
    has scoped keys like `@scope/tool` — and an installer shims the basename.  An entry
    that is empty or is only path punctuation names nothing.
    """
    if not isinstance(value, str):
        return None
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name if name and name not in {".", ".."} else None

def windows_command(name):
    lowered = name.lower()
    for suffix in WINDOWS_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name

def _is_command_name(value):
    """Reject the installer switches manifest authors put in command fields.

    winget's published index carries entries like `/VERYSILENT` and `/qn` beside real
    command names; they are silent-install flags, not executables.
    """
    return bool(value) and bool(COMMAND_NAME.fullmatch(value))

def scoop_manifests(manifests, source="scoop"):
    """Read Scoop's declared executables.

    A bucket manifest states its commands in `bin`, which is a string, a list of
    strings, or a list where a nested `[target, alias]` pair renames the shim.  The
    alias is what the user types, so it wins.
    """
    out=[]
    for package, value in manifests:
        entries=value.get("bin")
        if entries is None: continue
        if isinstance(entries,str): entries=[entries]
        if not isinstance(entries,list): continue
        version=value.get("version")
        homepage=value.get("homepage")
        commands=[]
        for entry in entries:
            if isinstance(entry,str): shim=entry
            elif isinstance(entry,list) and entry:
                shim=entry[1] if len(entry)>1 and isinstance(entry[1],str) and entry[1] else entry[0]
            else: continue
            if not isinstance(shim,str): continue
            name=windows_command(shim.replace("\\","/").rsplit("/",1)[-1].strip())
            if _is_command_name(name): commands.append(name)
        for command in sorted(set(commands)):
            out.append(record(command,"scoop",package,version,homepage,source,
                              source_type="os_package", package_system="scoop",
                              distribution_family="windows", distribution="windows",
                              latest_version=version))
    return out

def winget_commands(pairs, source="winget"):
    """Read winget's declared command aliases from its source index.

    The published source index carries a `commands` table joined to package
    identifiers, so the whole catalog's declared commands arrive in one download.
    """
    return [record(windows_command(command),"winget",package,None,None,source,
                   source_type="os_package", package_system="winget",
                   distribution_family="windows", distribution="windows")
            for command, package in sorted(set(pairs)) if _is_command_name(command) and package]

def npm_metadata(value, source="npm"):
    packages=value if isinstance(value,list) else [value]; out=[]
    for p in packages:
        bins=p.get("bin",{})
        if isinstance(bins,str): bins={p["name"].split("/")[-1]:bins}
        latest_version = p.get("version")
        times = p.get("time", {})
        latest_release_at = times.get(latest_version) if isinstance(times, dict) else None
        usage = p.get("downloads") or p.get("download_stats")
        for raw in bins:
            command = declared_command(raw)
            if not command: continue
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
        aliases = formula.get("aliases", {})
        # Homebrew's production API uses a list for formula aliases; those are
        # package names, not executable symlinks.  Only the normalized fixture
        # shape with explicit alias->target mappings contributes alias records.
        if isinstance(aliases, dict):
            for alias,target in aliases.items(): out.append(record(alias,"homebrew",package,version,formula.get("homepage"),source,"filesystem",target,
                source_type="os_package", package_system="homebrew", distribution_family="macos", distribution="macos"))
    return out
