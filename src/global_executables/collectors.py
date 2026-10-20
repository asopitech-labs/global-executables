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

# --- C and C++ recipe registries ---------------------------------------------------
# C and C++ have no single registry, and none of the three recipe repositories answers
# "which commands does this install?" the way npm or RubyGems do.  Each one is read for
# the strongest evidence it actually carries, and the confidence says which that was.
CPP_REPOSITORIES = {
    "vcpkg": "https://github.com/microsoft/vcpkg",
    "xmake": "https://github.com/xmake-io/xmake-repo",
    "conan": "https://github.com/conan-io/conan-center-index",
}
VCPKG_COPY_TOOLS = re.compile(r"vcpkg_copy_tools\s*\((.*?)\)", re.S)
# TOOL_NAMES runs until the next keyword of the same call.
VCPKG_TOOL_KEYWORDS = ("TOOL_NAMES", "AUTO_CLEAN", "SEARCH_DIR", "DESTINATION", "NO_SUFFIX")


CMAKE_BRACKET_OPEN = re.compile(r"\[(=*)\[")


def strip_cmake_comments(text):
    """Remove CMake line comments (`# ...`) and bracket comments (`#[[ ... ]]`).

    A portfile documents the tools it deliberately does not install, and comments out
    whole `vcpkg_copy_tools` calls, so a name inside a comment is not a declaration.
    Quoted arguments and bracket arguments are kept intact, because a `#` inside them
    does not start a comment.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            end = i + 1
            while end < n and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[i:end + 1])
            i = end + 1
        elif char == "#":
            bracket = CMAKE_BRACKET_OPEN.match(text, i + 1)
            if bracket:
                close = text.find("]" + bracket.group(1) + "]", bracket.end())
                i = n if close < 0 else close + len(bracket.group(1)) + 2
            else:
                newline = text.find("\n", i)
                i = n if newline < 0 else newline
        elif char == "[" and CMAKE_BRACKET_OPEN.match(text, i):
            bracket = CMAKE_BRACKET_OPEN.match(text, i)
            close = text.find("]" + bracket.group(1) + "]", bracket.end())
            end = n if close < 0 else close + len(bracket.group(1)) + 2
            out.append(text[i:end])
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)


def vcpkg_tool_names(portfile):
    """Read the commands a vcpkg port copies into `tools/<port>/`.

    `vcpkg_copy_tools` is what moves a built binary into the installed tool directory,
    so a port that installs a command has to name it here.  A name built from a CMake
    variable is not resolvable without running the port, so it is counted rather than
    guessed at.  Comments are removed first: `mnn` comments out a whole call and
    annotates names with `# tools/cpp`, and `openexr` lists `# not installed: exrcheck`.
    """
    names, unresolved = [], 0
    for block in VCPKG_COPY_TOOLS.findall(strip_cmake_comments(portfile)):
        collecting = False
        for token in block.split():
            bare = token.strip('"\'')
            if bare in VCPKG_TOOL_KEYWORDS:
                collecting = bare == "TOOL_NAMES"
                continue
            if not collecting:
                continue
            if "$" in bare or "@" in bare:
                unresolved += 1
                continue
            name = declared_command(bare)
            if name and COMMAND_NAME.match(name):
                names.append(windows_command(name))
    return sorted(set(names)), unresolved


def vcpkg_ports(ports, source="vcpkg"):
    """Build records from `(port, version, portfile)` triples."""
    out = []
    for port, version, portfile in ports:
        names, _ = vcpkg_tool_names(portfile)
        for command in names:
            out.append(record(command, "vcpkg", port, version, CPP_REPOSITORIES["vcpkg"], source,
                              "direct", source_type="language_package", language="c++",
                              package_system="vcpkg", registry="vcpkg", latest_version=version))
    return out


XMAKE_KIND = re.compile(r"""set_kind\s*\(\s*["'](\w+)["']""")
XMAKE_VERSION = re.compile(r"""add_versions\s*\(\s*["']([^"']+)["']""")
# `add_versions("github:1.10.0", ...)` names the URL alias the version is fetched from.
XMAKE_VERSION_ALIAS = re.compile(r"^[A-Za-z][\w-]*:")
# An xmake binary package never names its command, so the package name stands in for it.
# These packages are bundles or build-system helpers whose name is not a command any of
# them installs (`binutils` installs `ld` and `as`, `qt-tools` installs `moc` and `uic`),
# so inferring a command from the name would invent one.  The list is deliberately
# short: anything else keeps the `inferred` record.
XMAKE_NON_COMMAND_PACKAGES = frozenset({
    "autotools", "binutils", "gz-cmake", "jrl-cmakemodules", "policycoreutils",
    "shared-mime-info", "texinfo",
})
XMAKE_NON_COMMAND_SUFFIXES = ("-tools", "_tools")  # depot_tools, linux-tools, qt-tools, ...
PRERELEASE_TAGS = frozenset({"a", "alpha", "b", "beta", "dev", "pre", "preview", "rc", "snapshot"})


def version_sort_key(value):
    """Order version strings newest-last without assuming strict semver.

    Numeric runs compare as numbers, a release sorts after its pre-releases
    (`1.0.0-rc1` < `1.0.0` < `1.0.0.1`), and a leading `v` is ignored.
    """
    key = []
    for token in re.findall(r"\d+|[A-Za-z]+", value.lstrip("vV")):
        if token.isdigit():
            key.append((2, int(token), ""))
        elif token.lower() in PRERELEASE_TAGS:
            key.append((0, 0, token.lower()))
        else:
            key.append((1, 0, token.lower()))
    key.append((1, 0, ""))
    return tuple(key)


def xmake_newest_version(definition):
    """Return the newest version an xmake.lua declares.

    The order of `add_versions` lines is not meaningful: many packages list the newest
    first (`meson` declares 1.12.1 first and 0.50.1 last), so the last line is often the
    oldest.
    """
    versions = [XMAKE_VERSION_ALIAS.sub("", value) for value in XMAKE_VERSION.findall(definition)]
    versions = [value for value in versions if value]
    return max(versions, key=version_sort_key) if versions else None


def xmake_infers_command(package):
    return package not in XMAKE_NON_COMMAND_PACKAGES and not package.endswith(XMAKE_NON_COMMAND_SUFFIXES)


def xmake_packages(packages, source="xmake-repo"):
    """Build records from `(package, xmake.lua)` pairs.

    An xmake package declares that it installs a command through `set_kind("binary")`
    but never names it, and its install step is Lua that only a build can resolve.  The
    package name is the command in the common case and the record says `inferred`, so a
    consumer can tell this apart from a declaration or a file listing.
    """
    out = []
    for package, definition in packages:
        kind = XMAKE_KIND.search(definition)
        if not kind or kind.group(1) != "binary" or not xmake_infers_command(package):
            continue
        version = xmake_newest_version(definition)
        command = declared_command(package)
        if not command or not COMMAND_NAME.match(command):
            continue
        out.append(record(command, "xmake", package, version, CPP_REPOSITORIES["xmake"], source,
                          "inferred", source_type="language_package", language="c++",
                          package_system="xmake", registry="xmake-repo", latest_version=version))
    return out


CONAN_MANIFEST_BIN = re.compile(r"^bin/([^/:]+):", re.M)
# A built package puts more than commands under `bin/`: import libraries, debug
# symbols and the odd data file live there too, on Windows especially.
# `conanmanifest.txt` carries no executable bit, so these are filtered by name.  The
# lists are conservative: only names that are never invoked as a command are dropped,
# and scripts (`.py`, `.sh`, `.pl`) are kept because `bin/` scripts are commands.
CONAN_NON_COMMAND_SUFFIXES = (
    ".dll", ".so", ".dylib", ".lib", ".a", ".pdb", ".exp", ".ilk", ".def",
    ".txt", ".md", ".cmake", ".json", ".xml", ".yml", ".yaml", ".h", ".hpp", ".pc",
    # Configuration, packaging metadata, documentation and resources.
    ".cfg", ".conf", ".config", ".ini", ".toml", ".in", ".manifest", ".plist",
    ".html", ".htm", ".rst", ".jar", ".pyc", ".pyo", ".ico", ".png", ".mo", ".qm",
)
# Versioned shared libraries: `libfoo.so.1`, `libfoo.so.1.2.3`.
CONAN_SHARED_LIBRARY = re.compile(r"\.so(\.\d+)+$", re.I)
# Upper-case documentation and ownership files that projects drop next to their tools
# (`depot_tools` ships `OWNERS` and `LUCI_OWNERS`; `meson` ships `COPYING`, `PKG-INFO`).
CONAN_NON_COMMAND_NAMES = re.compile(
    r"^(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|README|AUTHORS|CHANGELOG|CHANGES|NEWS|"
    r"OWNERS|[A-Z]+_OWNERS|PKG-INFO|DIR_METADATA|MANIFEST)([._-].*)?$")


def conan_manifest_commands(manifest):
    """Read the commands a built ConanCenter package installs into `bin/`.

    `conanmanifest.txt` lists every file of the built package with its digest, so the
    command set is filesystem evidence that costs one small text file rather than the
    package archive.
    """
    names = []
    for entry in CONAN_MANIFEST_BIN.findall(manifest):
        name = entry.strip()
        if not name or name.startswith("."):
            continue
        if (name.lower().endswith(CONAN_NON_COMMAND_SUFFIXES) or CONAN_SHARED_LIBRARY.search(name)
                or CONAN_NON_COMMAND_NAMES.match(name)):
            continue
        command = declared_command(name)
        if command and COMMAND_NAME.match(command):
            names.append(windows_command(command))
    return sorted(set(names))
