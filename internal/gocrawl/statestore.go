package gocrawl

// The registry state is one logical JSON document shared with the Python crawlers.
// It is stored as a directory of small, canonical files so the published branch
// never holds one file near GitHub's size limit and a run rewrites only the shards
// it changed. src/global_executables/registry_state.py implements the same layout
// byte for byte; docs/OPERATIONS.md describes it.

import (
	"bytes"
	"cmp"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
)

const (
	stateLayoutFormat  = "global-executables-registry-state"
	stateLayoutVersion = 1
	stateManifestName  = "manifest.json"
	stateStagingName   = ".staging"
	stateStagedFiles   = "files"
	stateSourceFile    = "source.json"
	stateInlineLimit   = 1024
	stateShardTarget   = 8192
	stateMaxPrefix     = 3
	stateReadAttempts  = 8
)

var (
	stateSummaryFields = []string{
		"catalog_complete", "catalog_digest", "catalog_size", "catalog_snapshot",
		"cursor", "refresh_cursor", "snapshot_generation",
	}
	stateSourceName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
	stateFieldName  = regexp.MustCompile(`^[a-z0-9][a-z0-9_]*$`)
	errTornState    = errors.New("registry state changed while it was read")
)

type stateFileEntry struct {
	Bytes  int    `json:"bytes"`
	SHA256 string `json:"sha256"`
}

type stateFieldEntry struct {
	Entries      int `json:"entries"`
	PrefixLength int `json:"prefix_length"`
}

type stateSourceEntry struct {
	Fields  map[string]stateFieldEntry `json:"fields"`
	Summary map[string]any             `json:"summary"`
}

// Fields are declared in key order so the manifest matches Python's sort_keys output.
type stateManifest struct {
	Document      map[string]any              `json:"document"`
	Files         map[string]stateFileEntry   `json:"files"`
	Format        string                      `json:"format"`
	LayoutVersion int                         `json:"layout_version"`
	Sources       map[string]stateSourceEntry `json:"sources"`
}

// StateLayoutPaths maps a --state argument to the layout directory and the legacy
// single-file path. A path ending in .json names the legacy file.
func StateLayoutPaths(path string) (root, legacy string) {
	path = filepath.Clean(path)
	if trimmed, ok := strings.CutSuffix(path, ".json"); ok && filepath.Base(path) != ".json" {
		return trimmed, path
	}
	return path, path + ".json"
}

// StateExists reports whether either layout holds a state at path.
func StateExists(path string) bool {
	root, legacy := StateLayoutPaths(path)
	for _, candidate := range []string{
		filepath.Join(root, stateManifestName),
		filepath.Join(root, stateStagingName, stateManifestName),
		legacy,
	} {
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
			return true
		}
	}
	return false
}

func encodeCanonical(value any, indent bool) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if indent {
		encoder.SetIndent("", "  ")
	}
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	if indent {
		return buffer.Bytes(), nil
	}
	return bytes.TrimSuffix(buffer.Bytes(), []byte("\n")), nil
}

func decodeJSON(data []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("unexpected data after JSON value")
	}
	return value, nil
}

func statePrefixLength(entries int) int {
	length, capacity := 0, stateShardTarget
	for length < stateMaxPrefix && entries > capacity {
		length++
		capacity *= 16
	}
	return length
}

func stateShardName(key string, length int) string {
	if length == 0 {
		return "all.jsonl"
	}
	sum := sha256.Sum256([]byte(key))
	index := binary.BigEndian.Uint16(sum[:2]) >> (16 - 4*length)
	prefix := fmt.Appendf(nil, "%0*x", length, index)
	reversed := slices.Clone(prefix)
	slices.Reverse(reversed)
	// The reversed copy keeps Git's 16-character path hash unique per shard, so a
	// push sends deltas against each shard's previous version (see shard_file_name in
	// registry_state.py).
	return string(prefix) + "-" + string(reversed) + ".jsonl"
}

func encodeStateLayout(document map[string]any) (stateManifest, map[string][]byte, error) {
	manifest := stateManifest{
		Document:      map[string]any{},
		Files:         map[string]stateFileEntry{},
		Format:        stateLayoutFormat,
		LayoutVersion: stateLayoutVersion,
		Sources:       map[string]stateSourceEntry{},
	}
	files := map[string][]byte{}
	sources := map[string]any{}
	if value, exists := document["sources"]; exists {
		typed, ok := value.(map[string]any)
		if !ok {
			return manifest, nil, errors.New("registry state 'sources' must be a JSON object")
		}
		sources = typed
	}
	for key, value := range document {
		if key != "sources" {
			manifest.Document[key] = value
		}
	}
	for _, source := range slices.Sorted(maps.Keys(sources)) {
		if !stateSourceName.MatchString(source) {
			return manifest, nil, fmt.Errorf("registry source name cannot be stored as a directory: %q", source)
		}
		entry := sources[source]
		description := stateSourceEntry{Fields: map[string]stateFieldEntry{}, Summary: map[string]any{}}
		inline := entry
		if fields, ok := entry.(map[string]any); ok {
			kept := map[string]any{}
			for field, value := range fields {
				values, isMap := value.(map[string]any)
				if !isMap || len(values) < stateInlineLimit || !stateFieldName.MatchString(field) {
					kept[field] = value
					continue
				}
				length := statePrefixLength(len(values))
				description.Fields[field] = stateFieldEntry{Entries: len(values), PrefixLength: length}
				shards := map[string]*bytes.Buffer{}
				for _, key := range slices.Sorted(maps.Keys(values)) {
					line, err := encodeCanonical([]any{key, values[key]}, false)
					if err != nil {
						return manifest, nil, err
					}
					name := stateShardName(key, length)
					buffer := shards[name]
					if buffer == nil {
						buffer = &bytes.Buffer{}
						shards[name] = buffer
					}
					buffer.Write(line)
					buffer.WriteByte('\n')
				}
				for name, buffer := range shards {
					files[source+"/"+field+"/"+name] = buffer.Bytes()
				}
			}
			for _, name := range stateSummaryFields {
				value, exists := fields[name]
				if !exists {
					continue
				}
				switch value.(type) {
				case map[string]any, []any:
				default:
					description.Summary[name] = value
				}
			}
			inline = kept
		}
		body, err := encodeCanonical(inline, true)
		if err != nil {
			return manifest, nil, err
		}
		files[source+"/"+stateSourceFile] = body
		manifest.Sources[source] = description
	}
	for path, body := range files {
		sum := sha256.Sum256(body)
		manifest.Files[path] = stateFileEntry{Bytes: len(body), SHA256: hex.EncodeToString(sum[:])}
	}
	return manifest, files, nil
}

func parseStateManifest(body []byte, origin string) (stateManifest, error) {
	var manifest stateManifest
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := decoder.Decode(&manifest); err != nil {
		return manifest, fmt.Errorf("%w: %s: %w", errTornState, origin, err)
	}
	if manifest.Format != stateLayoutFormat || manifest.Files == nil || manifest.Sources == nil || manifest.Document == nil {
		return manifest, fmt.Errorf("not a registry state manifest: %s", origin)
	}
	if manifest.LayoutVersion != stateLayoutVersion {
		return manifest, fmt.Errorf("unsupported registry state layout %d in %s", manifest.LayoutVersion, origin)
	}
	for path := range manifest.Files {
		if strings.HasPrefix(path, "/") {
			return manifest, fmt.Errorf("unsafe path in registry state manifest: %q", path)
		}
		for part := range strings.SplitSeq(path, "/") {
			if part == "" || strings.HasPrefix(part, ".") {
				return manifest, fmt.Errorf("unsafe path in registry state manifest: %q", path)
			}
		}
	}
	return manifest, nil
}

func readVerified(candidates []string, expected stateFileEntry) ([]byte, error) {
	for _, candidate := range candidates {
		body, err := os.ReadFile(candidate)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256(body)
		if len(body) == expected.Bytes && hex.EncodeToString(sum[:]) == expected.SHA256 {
			return body, nil
		}
	}
	return nil, fmt.Errorf("%w: %s", errTornState, candidates[len(candidates)-1])
}

func loadStateLayout(root string) (map[string]any, bool, error) {
	stagedManifest := filepath.Join(root, stateStagingName, stateManifestName)
	origin := stagedManifest
	candidates := func(path string) []string {
		return []string{
			filepath.Join(root, stateStagingName, stateStagedFiles, filepath.FromSlash(path)),
			filepath.Join(root, filepath.FromSlash(path)),
		}
	}
	body, err := os.ReadFile(stagedManifest)
	if errors.Is(err, fs.ErrNotExist) {
		origin = filepath.Join(root, stateManifestName)
		body, err = os.ReadFile(origin)
		if errors.Is(err, fs.ErrNotExist) {
			if _, statErr := os.Stat(stagedManifest); statErr == nil {
				return nil, false, fmt.Errorf("%w: %s", errTornState, stagedManifest)
			}
			return nil, false, nil
		}
		candidates = func(path string) []string {
			return []string{filepath.Join(root, filepath.FromSlash(path))}
		}
	}
	if err != nil {
		return nil, false, err
	}
	manifest, err := parseStateManifest(body, origin)
	if err != nil {
		return nil, false, err
	}
	sources := make(map[string]any, len(manifest.Sources))
	for source, description := range manifest.Sources {
		sourcePath := source + "/" + stateSourceFile
		expected, listed := manifest.Files[sourcePath]
		if !listed {
			return nil, false, fmt.Errorf("registry state manifest lists %q without %s", source, sourcePath)
		}
		body, err := readVerified(candidates(sourcePath), expected)
		if err != nil {
			return nil, false, err
		}
		entry, err := decodeJSON(body)
		if err != nil {
			return nil, false, fmt.Errorf("%s: %w", sourcePath, err)
		}
		for field := range description.Fields {
			fields, ok := entry.(map[string]any)
			if !ok {
				return nil, false, fmt.Errorf("registry source %q has shards but is not an object", source)
			}
			merged := map[string]any{}
			prefix := source + "/" + field + "/"
			for _, path := range slices.Sorted(maps.Keys(manifest.Files)) {
				if !strings.HasPrefix(path, prefix) {
					continue
				}
				body, err := readVerified(candidates(path), manifest.Files[path])
				if err != nil {
					return nil, false, err
				}
				if err := decodeStateShard(body, path, merged); err != nil {
					return nil, false, err
				}
			}
			fields[field] = merged
		}
		sources[source] = entry
	}
	document := maps.Clone(manifest.Document)
	document["sources"] = sources
	return document, true, nil
}

func decodeStateShard(body []byte, origin string, into map[string]any) error {
	if len(body) == 0 || body[len(body)-1] != '\n' {
		return fmt.Errorf("truncated registry state shard: %s", origin)
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	for {
		var pair []any
		err := decoder.Decode(&pair)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("%s: %w", origin, err)
		}
		key, ok := pair[0].(string)
		if len(pair) != 2 || !ok {
			return fmt.Errorf("%s: shard lines must be [key, value] pairs", origin)
		}
		into[key] = pair[1]
	}
}

// ReadStateDocument reads the directory layout, falling back to the legacy single
// file while no directory layout exists. found is false when neither exists.
func ReadStateDocument(path string) (document StateDocument, found bool, err error) {
	root, legacy := StateLayoutPaths(path)
	for attempt := range stateReadAttempts {
		generic, exists, err := loadStateLayout(root)
		if errors.Is(err, errTornState) {
			time.Sleep(time.Duration(attempt+1) * 50 * time.Millisecond)
			continue
		}
		if err != nil {
			return nil, false, err
		}
		if !exists {
			body, err := os.ReadFile(legacy)
			if errors.Is(err, fs.ErrNotExist) {
				return StateDocument{}, false, nil
			}
			if err != nil {
				return nil, false, err
			}
			document := StateDocument{}
			if err := json.Unmarshal(body, &document); err != nil {
				return nil, false, fmt.Errorf("read state: %w", err)
			}
			return document, true, nil
		}
		document := make(StateDocument, len(generic))
		for key, value := range generic {
			raw, err := encodeCanonical(value, false)
			if err != nil {
				return nil, false, err
			}
			document[key] = raw
		}
		return document, true, nil
	}
	return nil, false, fmt.Errorf("registry state at %s kept changing while it was read", root)
}

// WriteStateDocument stores document in the directory layout at path, rewriting
// only the files whose content changed, and removes the legacy single file.
func WriteStateDocument(path string, document StateDocument) error {
	root, legacy := StateLayoutPaths(path)
	generic := make(map[string]any, len(document))
	for key, raw := range document {
		value, err := decodeJSON(raw)
		if err != nil {
			return fmt.Errorf("state key %q: %w", key, err)
		}
		generic[key] = value
	}
	manifest, files, err := encodeStateLayout(generic)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return err
	}
	if err := RecoverStateLayout(path); err != nil {
		return err
	}
	manifestPath := filepath.Join(root, stateManifestName)
	previous := map[string]stateFileEntry{}
	existing, readErr := os.ReadFile(manifestPath)
	if readErr == nil {
		if parsed, err := parseStateManifest(existing, manifestPath); err == nil {
			previous = parsed.Files
		}
	}
	changed := []string{}
	for _, relative := range slices.Sorted(maps.Keys(files)) {
		expected := manifest.Files[relative]
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(relative)))
		if old, ok := previous[relative]; ok && old == expected && err == nil && info.Mode().IsRegular() && info.Size() == int64(expected.Bytes) {
			continue
		}
		changed = append(changed, relative)
	}
	manifestBody, err := encodeCanonical(manifest, true)
	if err != nil {
		return err
	}
	if len(changed) == 0 && readErr == nil && bytes.Equal(existing, manifestBody) {
		if err := collectStateGarbage(root, manifest.Files); err != nil {
			return err
		}
	} else {
		staged := filepath.Join(root, stateStagingName, stateStagedFiles)
		for _, relative := range changed {
			if err := atomicWrite(filepath.Join(staged, filepath.FromSlash(relative)), files[relative], 0o644); err != nil {
				return err
			}
		}
		if err := atomicWrite(filepath.Join(root, stateStagingName, stateManifestName), manifestBody, 0o644); err != nil {
			return err
		}
		if err := applyStateStaging(root); err != nil {
			return err
		}
	}
	if err := os.Remove(legacy); err == nil {
		return syncDirectory(filepath.Dir(legacy))
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// RecoverStateLayout finishes a committed save that was interrupted, or discards an
// uncommitted one.
func RecoverStateLayout(path string) error {
	root, _ := StateLayoutPaths(path)
	staging := filepath.Join(root, stateStagingName)
	if info, err := os.Stat(filepath.Join(staging, stateManifestName)); err == nil && info.Mode().IsRegular() {
		return applyStateStaging(root)
	}
	if err := os.RemoveAll(staging); err != nil {
		return err
	}
	return nil
}

func applyStateStaging(root string) error {
	staging := filepath.Join(root, stateStagingName)
	stagedManifest := filepath.Join(staging, stateManifestName)
	body, err := os.ReadFile(stagedManifest)
	if err != nil {
		return err
	}
	manifest, err := parseStateManifest(body, stagedManifest)
	if err != nil {
		return err
	}
	staged := filepath.Join(staging, stateStagedFiles)
	touched := map[string]bool{}
	err = filepath.WalkDir(staged, func(path string, entry fs.DirEntry, err error) error {
		if errors.Is(err, fs.ErrNotExist) && path == staged {
			return filepath.SkipDir
		}
		if err != nil || entry.IsDir() {
			return err
		}
		if strings.HasPrefix(entry.Name(), ".") {
			return os.Remove(path)
		}
		relative, err := filepath.Rel(staged, path)
		if err != nil {
			return err
		}
		target := filepath.Join(root, relative)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		touched[filepath.Dir(target)] = true
		return os.Rename(path, target)
	})
	if err != nil {
		return err
	}
	for _, directory := range slices.Sorted(maps.Keys(touched)) {
		if err := syncDirectory(directory); err != nil {
			return err
		}
	}
	if err := os.Rename(stagedManifest, filepath.Join(root, stateManifestName)); err != nil {
		return err
	}
	if err := syncDirectory(root); err != nil {
		return err
	}
	if err := collectStateGarbage(root, manifest.Files); err != nil {
		return err
	}
	return os.RemoveAll(staging)
}

func collectStateGarbage(root string, listed map[string]stateFileEntry) error {
	directories := []string{}
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		if entry.IsDir() {
			if relative == stateStagingName {
				return filepath.SkipDir
			}
			if relative != "." {
				directories = append(directories, path)
			}
			return nil
		}
		if _, ok := listed[relative]; ok || relative == stateManifestName {
			return nil
		}
		return os.Remove(path)
	})
	if err != nil {
		return err
	}
	// Remove emptied directories deepest first.
	slices.SortFunc(directories, func(left, right string) int { return cmp.Compare(len(right), len(left)) })
	for _, directory := range directories {
		entries, err := os.ReadDir(directory)
		if err != nil {
			return err
		}
		if len(entries) == 0 {
			if err := os.Remove(directory); err != nil {
				return err
			}
		}
	}
	return nil
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}
