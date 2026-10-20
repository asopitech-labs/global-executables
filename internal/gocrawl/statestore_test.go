package gocrawl

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const stateFixtureDirectory = "testdata/registry-state"

func readStateForTest(t *testing.T, path string) []byte {
	t.Helper()
	document, found, err := ReadStateDocument(path)
	if err != nil || !found {
		t.Fatalf("read state %s: found=%v err=%v", path, found, err)
	}
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return body
}

// writeLegacyFixture writes the trimmed artifact-data sample (real go, pypi, conan ...
// checkpoints plus an edge-case source) as a legacy registry-state.json.
func writeLegacyFixture(t *testing.T, directory string) (string, []byte) {
	t.Helper()
	compressed, err := os.Open(filepath.Join(stateFixtureDirectory, "legacy-registry-state.json.gz"))
	if err != nil {
		t.Fatal(err)
	}
	defer compressed.Close()
	reader, err := gzip.NewReader(compressed)
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "registry-state.json")
	if err := os.WriteFile(path, body, 0o644); err != nil {
		t.Fatal(err)
	}
	return path, body
}

func canonicalDocument(t *testing.T, document StateDocument) string {
	t.Helper()
	generic := map[string]any{}
	for key, raw := range document {
		value, err := decodeJSON(raw)
		if err != nil {
			t.Fatal(err)
		}
		generic[key] = value
	}
	body, err := encodeCanonical(generic, false)
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}

func layoutFiles(t *testing.T, root string) map[string]string {
	t.Helper()
	files := map[string]string{}
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		relative, _ := filepath.Rel(root, path)
		body, err := os.ReadFile(path)
		files[filepath.ToSlash(relative)] = string(body)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return files
}

func TestStateLayoutMigratesLegacyFileToGoldenLayout(t *testing.T) {
	directory := t.TempDir()
	legacyPath, legacyBody := writeLegacyFixture(t, directory)
	document, err := LoadStateDocument(legacyPath)
	if err != nil {
		t.Fatal(err)
	}
	var legacy StateDocument
	if err := json.Unmarshal(legacyBody, &legacy); err != nil {
		t.Fatal(err)
	}
	if err := WriteStateDocument(legacyPath, document); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(legacyPath); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("legacy file survived migration: %v", err)
	}
	root := filepath.Join(directory, "registry-state")
	manifest, _ := os.ReadFile(filepath.Join(root, stateManifestName))
	golden, _ := os.ReadFile(filepath.Join(stateFixtureDirectory, "manifest.golden.json"))
	if !bytes.Equal(manifest, golden) {
		t.Fatalf("Go layout differs from the golden manifest Python produces:\n%s", manifest)
	}
	files := layoutFiles(t, root)
	if _, staged := files[".staging/"+stateManifestName]; staged || len(files) != 28 {
		t.Fatalf("unexpected layout files: %v", slices.Sorted(maps.Keys(files)))
	}
	for _, name := range []string{"go/unavailable/0-0.jsonl", "go/unavailable/f-f.jsonl", "pypi/unavailable/all.jsonl", "conan/source.json"} {
		if _, ok := files[name]; !ok {
			t.Fatalf("missing %s in %v", name, slices.Sorted(maps.Keys(files)))
		}
	}
	reread, found, err := ReadStateDocument(root)
	if err != nil || !found {
		t.Fatalf("found=%v err=%v", found, err)
	}
	if canonicalDocument(t, reread) != canonicalDocument(t, legacy) {
		t.Fatal("round trip through the layout changed the document")
	}
}

func TestStateLayoutIsDeterministicAndRewritesOnlyChangedShards(t *testing.T) {
	directory := t.TempDir()
	legacyPath, _ := writeLegacyFixture(t, directory)
	document, err := LoadStateDocument(legacyPath)
	if err != nil {
		t.Fatal(err)
	}
	first, second := filepath.Join(directory, "first"), filepath.Join(directory, "second")
	if err := WriteStateDocument(first, document); err != nil {
		t.Fatal(err)
	}
	reread, _, err := ReadStateDocument(first)
	if err != nil {
		t.Fatal(err)
	}
	if err := WriteStateDocument(second, reread); err != nil {
		t.Fatal(err)
	}
	before := layoutFiles(t, first)
	if !maps.Equal(before, layoutFiles(t, second)) {
		t.Fatal("re-encoding a read document produced different bytes")
	}
	stats := map[string]os.FileInfo{}
	for name := range before {
		info, _ := os.Stat(filepath.Join(first, name))
		stats[name] = info
	}

	var sources map[string]map[string]json.RawMessage
	if err := json.Unmarshal(reread["sources"], &sources); err != nil {
		t.Fatal(err)
	}
	var unavailable map[string]string
	if err := json.Unmarshal(sources["go"]["unavailable"], &unavailable); err != nil {
		t.Fatal(err)
	}
	unavailable["zz.example/added"] = "HTTP 404: Not Found"
	sources["go"]["unavailable"], _ = json.Marshal(unavailable)
	reread["sources"], _ = json.Marshal(sources)
	if err := WriteStateDocument(first, reread); err != nil {
		t.Fatal(err)
	}
	after := layoutFiles(t, first)
	changed := []string{}
	for name, body := range after {
		if before[name] != body {
			changed = append(changed, name)
		}
	}
	slices.Sort(changed)
	shard := "go/unavailable/" + stateShardName("zz.example/added", 1)
	if !slices.Equal(changed, []string{shard, stateManifestName}) {
		t.Fatalf("changed=%v, want only %s and the manifest", changed, shard)
	}
	for name, info := range stats {
		if name == shard || name == stateManifestName {
			continue
		}
		current, _ := os.Stat(filepath.Join(first, name))
		if !os.SameFile(info, current) {
			t.Fatalf("unchanged file %s was rewritten", name)
		}
	}
}

func TestStateLayoutRecoversFromInterruptedSaves(t *testing.T) {
	directory := t.TempDir()
	root := filepath.Join(directory, "registry-state")
	old := StateDocument{"version": json.RawMessage("1"), "sources": json.RawMessage(`{"npm":{"cursor":1}}`)}
	next := StateDocument{"version": json.RawMessage("1"), "sources": json.RawMessage(`{"npm":{"cursor":2}}`)}
	if err := WriteStateDocument(root, old); err != nil {
		t.Fatal(err)
	}
	// Uncommitted: staged files without a staged manifest are ignored and discarded.
	staged := filepath.Join(root, stateStagingName, stateStagedFiles, "npm", stateSourceFile)
	if err := os.MkdirAll(filepath.Dir(staged), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(staged, []byte("{\"cursor\": 9"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := readStateForTest(t, root); !bytes.Contains(got, []byte(`"cursor": 1`)) {
		t.Fatalf("uncommitted staging leaked: %s", got)
	}
	if err := RecoverStateLayout(root); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, stateStagingName)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatal("uncommitted staging was not discarded")
	}

	// Committed: a crash after the staged manifest but before the swap rolls forward,
	// for readers immediately and on disk at the next recovery.
	generic := map[string]any{}
	for key, raw := range next {
		generic[key], _ = decodeJSON(raw)
	}
	manifest, files, err := encodeStateLayout(generic)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(staged), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(staged, files["npm/"+stateSourceFile], 0o644); err != nil {
		t.Fatal(err)
	}
	manifestBody, _ := encodeCanonical(manifest, true)
	if err := os.WriteFile(filepath.Join(root, stateStagingName, stateManifestName), manifestBody, 0o644); err != nil {
		t.Fatal(err)
	}
	if got := readStateForTest(t, root); !bytes.Contains(got, []byte(`"cursor": 2`)) {
		t.Fatalf("committed staging not visible to readers: %s", got)
	}
	// Half applied: the shard already moved, the manifest not yet swapped.
	if err := os.Rename(staged, filepath.Join(root, "npm", stateSourceFile)); err != nil {
		t.Fatal(err)
	}
	if got := readStateForTest(t, root); !bytes.Contains(got, []byte(`"cursor": 2`)) {
		t.Fatalf("half-applied staging not visible to readers: %s", got)
	}
	if err := RecoverStateLayout(root); err != nil {
		t.Fatal(err)
	}
	if got := readStateForTest(t, root); !bytes.Contains(got, []byte(`"cursor": 2`)) {
		t.Fatalf("recovery lost the committed save: %s", got)
	}
	if _, err := os.Stat(filepath.Join(root, stateStagingName)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatal("staging survived recovery")
	}
}

func TestStateLayoutRejectsFilesThatDoNotMatchTheManifest(t *testing.T) {
	directory := t.TempDir()
	root := filepath.Join(directory, "registry-state")
	document := StateDocument{"version": json.RawMessage("1"), "sources": json.RawMessage(`{"npm":{"cursor":1}}`)}
	if err := WriteStateDocument(root, document); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "npm", stateSourceFile), []byte("{\"cursor\": 5}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ReadStateDocument(root); err == nil || !strings.Contains(err.Error(), "kept changing") {
		t.Fatalf("torn read accepted: %v", err)
	}
}

func TestStateLayoutPathsAcceptLegacyAndDirectoryArguments(t *testing.T) {
	for _, path := range []string{"data/production/registry-state.json", "data/production/registry-state", "data/production/registry-state/"} {
		root, legacy := StateLayoutPaths(path)
		if root != filepath.FromSlash("data/production/registry-state") || legacy != filepath.FromSlash("data/production/registry-state.json") {
			t.Fatalf("%s -> %s, %s", path, root, legacy)
		}
	}
	if document, found, err := ReadStateDocument(filepath.Join(t.TempDir(), "missing")); err != nil || found || len(document) != 0 {
		t.Fatalf("missing state: %v %v %v", document, found, err)
	}
}

// Go writes, Python reads and rewrites, Go reads: the bytes must not change. Skipped
// where python3 is unavailable (the golden manifest still pins both encoders).
func TestStateLayoutInteroperatesWithPython(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 not available")
	}
	source, _ := filepath.Abs("../../src")
	if _, err := os.Stat(filepath.Join(source, "global_executables", "registry_state.py")); err != nil {
		t.Skip("Python sources are not in this build context")
	}
	directory := t.TempDir()
	legacyPath, _ := writeLegacyFixture(t, directory)
	document, err := LoadStateDocument(legacyPath)
	if err != nil {
		t.Fatal(err)
	}
	goRoot, pythonRoot := filepath.Join(directory, "go"), filepath.Join(directory, "python")
	if err := WriteStateDocument(goRoot, document); err != nil {
		t.Fatal(err)
	}
	script := "import sys; sys.path.insert(0, sys.argv[1])\n" +
		"from global_executables.registry_state import load_state, save_state\n" +
		"save_state(sys.argv[3], load_state(sys.argv[2]))\n"
	command := exec.CommandContext(t.Context(), python, "-c", script, source, goRoot, pythonRoot)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("python round trip: %v\n%s", err, output)
	}
	if !maps.Equal(layoutFiles(t, goRoot), layoutFiles(t, pythonRoot)) {
		t.Fatal("Python rewrote a Go-written layout with different bytes")
	}
	reread, _, err := ReadStateDocument(pythonRoot)
	if err != nil {
		t.Fatal(err)
	}
	if canonicalDocument(t, reread) != canonicalDocument(t, document) {
		t.Fatal("Go read a Python-written layout differently")
	}
}
