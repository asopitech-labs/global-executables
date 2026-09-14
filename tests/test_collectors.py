import json
from pathlib import Path
from jsonschema import Draft202012Validator
from global_executables.collectors import (conan_manifest_commands, crates_manifest, homebrew_metadata,
                                           npm_metadata, package_files, vcpkg_ports, vcpkg_tool_names,
                                           xmake_packages)
ROOT=Path(__file__).parents[1]/"fixtures/collectors"

def test_filesystem_collectors_only_bin_paths():
    deb=package_files((ROOT/"debian.txt").read_text(),"debian","fixture")
    arch=package_files((ROOT/"arch.txt").read_text(),"arch","fixture")
    assert {r["command"] for r in deb}=={"git","envcp"}
    assert [r["command"] for r in arch]==["curl"]
def test_language_collectors_use_declared_bins():
    npm=npm_metadata(json.loads((ROOT/"npm.json").read_text()))
    assert (npm[0]["package"],npm[0]["command"])==("foo-tool","foocli")
    c=json.loads((ROOT/"crates.json").read_text())
    assert crates_manifest(c["manifest"],c["package"])[0]["command"]=="rcli"
def test_homebrew_bottle_inventory_and_alias():
    rows=homebrew_metadata(json.loads((ROOT/"homebrew.json").read_text()))
    assert {r["command"] for r in rows}=={"git","git-old"}
    assert next(r for r in rows if r["command"]=="git-old")["alias_of"]=="git"


def _cpp_rows():
    """C/C++ recipes, read for the strongest evidence each repository carries."""
    return (vcpkg_ports([("toolkit", "1.4.0", (ROOT / "vcpkg-portfile.cmake").read_text()),
                         ("headers", "2.0.0", (ROOT / "vcpkg-library-portfile.cmake").read_text())],
                        "fixture") +
            xmake_packages([("demotool", (ROOT / "xmake-binary.lua").read_text()),
                            ("demolib", (ROOT / "xmake-library.lua").read_text())], "fixture"))


def test_vcpkg_reads_declared_tools_and_counts_what_cmake_hides():
    names, unresolved = vcpkg_tool_names((ROOT / "vcpkg-portfile.cmake").read_text())
    assert names == ["toolkit-dump", "toolkit-run"]
    # A name built from a CMake variable is counted, never guessed at.
    assert unresolved == 1
    assert vcpkg_tool_names((ROOT / "vcpkg-library-portfile.cmake").read_text()) == ([], 0)


def test_xmake_binary_packages_are_inferred_and_libraries_are_not_recorded():
    rows = xmake_packages([("demotool", (ROOT / "xmake-binary.lua").read_text()),
                           ("demolib", (ROOT / "xmake-library.lua").read_text())], "fixture")
    assert [(row["command"], row["confidence"], row["version"]) for row in rows] == [
        ("demotool", "inferred", "1.3.1")]


def test_conan_commands_come_from_the_built_package_file_list():
    manifest = (ROOT / "conanmanifest.txt").read_text()
    assert conan_manifest_commands(manifest) == ["demotool", "demotool-1.2"]
    assert conan_manifest_commands((ROOT / "conanmanifest-headeronly.txt").read_text()) == []


def test_cpp_recipe_rows_carry_ecosystem_and_evidence_provenance():
    rows = _cpp_rows()
    assert {row["ecosystem"] for row in rows} == {"vcpkg", "xmake"}
    vcpkg = [row for row in rows if row["ecosystem"] == "vcpkg"]
    assert {row["confidence"] for row in vcpkg} == {"direct"}
    assert {row["package"] for row in vcpkg} == {"toolkit"}
    assert all(row["language"] == "c++" and row["repository"] for row in rows)


def test_collectors_are_deterministic_and_intermediate_schema_valid():
    schema=json.loads((ROOT.parent.parent/"schema/intermediate.schema.json").read_text())
    validator=Draft202012Validator(schema)
    def collect():
        return (package_files((ROOT/"debian.txt").read_text(),"debian","fixture")+
                package_files((ROOT/"arch.txt").read_text(),"arch","fixture")+
                npm_metadata(json.loads((ROOT/"npm.json").read_text()))+
                homebrew_metadata(json.loads((ROOT/"homebrew.json").read_text()))+
                _cpp_rows())
    rows=collect()
    assert [json.dumps(row,sort_keys=True) for row in rows] == [json.dumps(row,sort_keys=True) for row in collect()]
    for row in rows:
        validator.validate(row)
