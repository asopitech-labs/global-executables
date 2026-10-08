import json
from pathlib import Path
from jsonschema import Draft202012Validator
from global_executables.collectors import (conan_manifest_commands, crates_manifest, homebrew_metadata,
                                           npm_metadata, package_files, strip_cmake_comments, vcpkg_ports,
                                           vcpkg_tool_names, version_sort_key, xmake_packages)
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


def test_vcpkg_ignores_names_inside_cmake_comments():
    # Real portfiles: mnn comments out a whole vcpkg_copy_tools call and annotates names
    # with `# tools/cpp`; openexr lists `# not installed: exrcheck` inside TOOL_NAMES.
    mnn, unresolved = vcpkg_tool_names((ROOT / "vcpkg-mnn-portfile.cmake").read_text())
    assert unresolved == 0
    assert {"cpp", "converter", "evaluation", "quantization", "train", "test"}.isdisjoint(mnn)
    assert {"run_test.out", "benchmark.out", "benchmarkExprModels.out"}.isdisjoint(mnn)
    assert {"MNNConvert", "MNNDump2Json", "TestConvertResult", "train.out"} <= set(mnn)
    openexr, _ = vcpkg_tool_names((ROOT / "vcpkg-openexr-portfile.cmake").read_text())
    assert "not" not in openexr and "exrcheck" not in openexr
    assert openexr[:2] == ["exr2aces", "exrenvmap"] and len(openexr) == 11


def test_cmake_comment_stripping_keeps_quoted_and_bracket_arguments():
    text = 'a "x # y" # line\n#[[ block\n # ]] b [=[ k # ]=] c #[==[ z ]==]d'
    assert strip_cmake_comments(text) == 'a "x # y" \n b [=[ k # ]=] c d'
    assert vcpkg_tool_names("vcpkg_copy_tools(TOOL_NAMES one #[[ two ]] three AUTO_CLEAN)") == (
        ["one", "three"], 0)


def test_xmake_binary_packages_are_inferred_and_libraries_are_not_recorded():
    rows = xmake_packages([("demotool", (ROOT / "xmake-binary.lua").read_text()),
                           ("demolib", (ROOT / "xmake-library.lua").read_text())], "fixture")
    assert [(row["command"], row["confidence"], row["version"]) for row in rows] == [
        ("demotool", "inferred", "1.3.1")]


def test_xmake_records_the_newest_declared_version_not_the_last_line():
    # Real meson recipe: 1.12.1 is declared first and 0.50.1 last.
    rows = xmake_packages([("meson", (ROOT / "xmake-meson.lua").read_text())], "fixture")
    assert [(row["command"], row["version"]) for row in rows] == [("meson", "1.12.1")]
    doxygen = ('package("doxygen")\n    set_kind("binary")\n'
               '    add_versions("archive:1.9.6", "aa")\n    add_versions("github:1.10.0", "Release_1_10_0")\n')
    assert xmake_packages([("doxygen", doxygen)])[0]["version"] == "1.10.0"
    assert sorted(["1.0.0", "1.0.0-rc1", "v1.0.1", "0.9", "1.0.0.1", "1.10.0", "1.9.9"],
                  key=version_sort_key) == ["0.9", "1.0.0-rc1", "1.0.0", "1.0.0.1", "v1.0.1", "1.9.9", "1.10.0"]


def test_xmake_does_not_infer_a_command_from_a_bundle_package_name():
    binary = 'package("{0}")\n    set_kind("binary")\n    add_versions("1.0", "aa")\n'
    names = ["autotools", "binutils", "linux-tools", "qt-tools", "vulkan-tools", "depot_tools",
             "texinfo", "ninja"]
    rows = xmake_packages([(name, binary.format(name)) for name in names])
    assert [row["command"] for row in rows] == ["ninja"]


def test_conan_commands_come_from_the_built_package_file_list():
    manifest = (ROOT / "conanmanifest.txt").read_text()
    assert conan_manifest_commands(manifest) == ["demotool", "demotool-1.2"]
    assert conan_manifest_commands((ROOT / "conanmanifest-headeronly.txt").read_text()) == []


def test_conan_drops_bin_entries_that_are_never_commands():
    # Names taken from published ConanCenter rows: meson shipped COPYING, PKG-INFO,
    # setup.cfg and pyproject.toml in bin/, depot_tools LICENSE and OWNERS files,
    # dependencies *.exe.config, maven m2.conf and zserio a jar.
    commands = conan_manifest_commands((ROOT / "conanmanifest-noncommands.txt").read_text())
    # Scripts and real executables stay; a Windows suffix is removed as before.
    assert commands == ["Dependencies", "gclient.py", "meson", "meson.py"]


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
