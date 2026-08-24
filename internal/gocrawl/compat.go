package gocrawl

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"time"
)

type StateDocument map[string]json.RawMessage

type ExportPaths struct {
	State        string
	Observations string
	Report       string
}

type PassReport struct {
	StartedAt         time.Time
	FinishedAt        time.Time
	Processed         uint64
	Records           uint64
	DownloadedBytes   uint64
	BudgetExhausted   bool
	Interrupted       bool
	Error             string
	Workers           int
	CatalogDiscovered uint64
	CatalogRequests   uint64
	CatalogError      string
	Complete          bool
}

type compatibilityState struct {
	Cursor          uint64            `json:"cursor"`
	CatalogSize     uint64            `json:"catalog_size"`
	CatalogComplete bool              `json:"catalog_complete"`
	CatalogSince    string            `json:"catalog_since"`
	ModulesFile     string            `json:"modules_file"`
	Failures        map[string]string `json:"failures"`
	FailureAttempts map[string]int    `json:"failure_attempts"`
	RetryModules    []string          `json:"retry_modules"`
	Unavailable     map[string]string `json:"unavailable"`
}

func LoadCompatibility(statePath, observationsPath, catalogPath string) (ImportSnapshot, StateDocument, error) {
	document, err := LoadStateDocument(statePath)
	if err != nil {
		return ImportSnapshot{}, nil, err
	}
	var sources map[string]json.RawMessage
	if raw := document["sources"]; len(raw) > 0 {
		if err := json.Unmarshal(raw, &sources); err != nil {
			return ImportSnapshot{}, nil, fmt.Errorf("read state sources: %w", err)
		}
	}
	if sources == nil {
		sources = make(map[string]json.RawMessage)
	}
	var state compatibilityState
	if raw := sources["go"]; len(raw) > 0 {
		if err := json.Unmarshal(raw, &state); err != nil {
			return ImportSnapshot{}, nil, fmt.Errorf("read Go state: %w", err)
		}
	}
	catalogSize, err := CountCatalog(catalogPath)
	if err != nil {
		return ImportSnapshot{}, nil, err
	}
	if state.Cursor > catalogSize {
		return ImportSnapshot{}, nil, fmt.Errorf("state cursor %d exceeds catalog size %d", state.Cursor, catalogSize)
	}
	offset, err := LocateCatalogOffset(catalogPath, state.Cursor)
	if err != nil {
		return ImportSnapshot{}, nil, err
	}
	retries := make(map[string]RetryEntry)
	for module, reason := range state.Failures {
		attempts := state.FailureAttempts[module]
		if attempts <= 0 {
			attempts = 1
		}
		retries[module] = RetryEntry{Error: reason, Attempts: attempts}
	}
	for _, module := range state.RetryModules {
		if _, exists := retries[module]; !exists {
			retries[module] = RetryEntry{Error: state.Failures[module], Attempts: max(1, state.FailureAttempts[module])}
		}
	}
	observations, err := loadObservations(observationsPath)
	if err != nil {
		return ImportSnapshot{}, nil, err
	}
	if state.Unavailable == nil {
		state.Unavailable = make(map[string]string)
	}
	return ImportSnapshot{
		Cursor: state.Cursor, CatalogOffset: offset, CatalogSize: catalogSize,
		CatalogComplete: state.CatalogComplete, CatalogSince: state.CatalogSince,
		ModulesFile: state.ModulesFile, Retries: retries, Unavailable: state.Unavailable,
		Observations: observations,
	}, document, nil
}

func LoadStateDocument(statePath string) (StateDocument, error) {
	document := StateDocument{}
	if body, err := os.ReadFile(statePath); err == nil {
		if err := json.Unmarshal(body, &document); err != nil {
			return nil, fmt.Errorf("read state: %w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if len(document) == 0 {
		document["version"] = json.RawMessage("1")
		document["sources"] = json.RawMessage("{}")
	}
	return document, nil
}

func loadObservations(path string) ([]Observation, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	observations := make([]Observation, 0)
	for scanner.Scan() {
		if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
			continue
		}
		var observation Observation
		if err := json.Unmarshal(scanner.Bytes(), &observation); err != nil {
			return nil, fmt.Errorf("read observation: %w", err)
		}
		observations = append(observations, observation)
	}
	return observations, scanner.Err()
}

func ExportCompatibility(paths ExportPaths, document StateDocument, snapshot Snapshot, pass PassReport) error {
	observations := func(yield func(Observation) error) error {
		for _, observation := range snapshot.Observations {
			if err := yield(observation); err != nil {
				return err
			}
		}
		return nil
	}
	return exportCompatibility(paths, document, snapshot, observations, pass)
}

func ExportStoreCompatibility(
	ctx context.Context,
	paths ExportPaths,
	document StateDocument,
	store *BoltStore,
	pass PassReport,
) error {
	return store.ViewSnapshot(ctx, func(snapshot Snapshot, observations ObservationSequence) error {
		return exportCompatibility(paths, document, snapshot, observations, pass)
	})
}

func exportCompatibility(
	paths ExportPaths,
	document StateDocument,
	snapshot Snapshot,
	observations ObservationSequence,
	pass PassReport,
) error {
	if document == nil {
		document = StateDocument{}
	}
	var sources map[string]json.RawMessage
	if raw := document["sources"]; len(raw) > 0 {
		if err := json.Unmarshal(raw, &sources); err != nil {
			return err
		}
	}
	if sources == nil {
		sources = make(map[string]json.RawMessage)
	}
	failures := make(map[string]string, len(snapshot.Retries))
	attempts := make(map[string]int, len(snapshot.Retries))
	retryModules := make([]string, 0, len(snapshot.Retries))
	for module, entry := range snapshot.Retries {
		failures[module] = entry.Error
		attempts[module] = entry.Attempts
		retryModules = append(retryModules, module)
	}
	sort.Strings(retryModules)
	goState := map[string]any{
		"catalog_complete":    snapshot.CatalogComplete,
		"catalog_size":        snapshot.CatalogSize,
		"catalog_since":       snapshot.CatalogSince,
		"cursor":              snapshot.Cursor,
		"failure_attempts":    attempts,
		"failures":            failures,
		"modules_file":        snapshot.ModulesFile,
		"retry_modules":       retryModules,
		"retry_projects":      []string{},
		"snapshot_generation": snapshot.Generation,
		"unavailable":         snapshot.Unavailable,
	}
	goRaw, err := json.Marshal(goState)
	if err != nil {
		return err
	}
	sources["go"] = goRaw
	sourcesRaw, err := json.Marshal(sources)
	if err != nil {
		return err
	}
	document["sources"] = sourcesRaw
	if _, exists := document["version"]; !exists {
		document["version"] = json.RawMessage("1")
	}
	stateBody, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return err
	}
	stateBody = append(stateBody, '\n')

	complete := snapshot.CatalogComplete && snapshot.Cursor >= snapshot.CatalogSize && len(snapshot.Retries) == 0
	coverage := "partial"
	if complete {
		coverage = "exhaustive"
	}
	status := "success"
	if pass.Error != "" {
		status = "failed"
	}
	duration := pass.FinishedAt.Sub(pass.StartedAt).Seconds()
	modulesPerMinute := float64(0)
	if duration > 0 {
		modulesPerMinute = float64(pass.Processed) * 60 / duration
	}
	sourceReport := map[string]any{
		"budget_exhausted":    pass.BudgetExhausted,
		"catalog_complete":    snapshot.CatalogComplete,
		"catalog_size":        snapshot.CatalogSize,
		"catalog_discovered":  pass.CatalogDiscovered,
		"catalog_requests":    pass.CatalogRequests,
		"catalog_error":       pass.CatalogError,
		"complete":            complete,
		"coverage_kind":       coverage,
		"cursor":              snapshot.Cursor,
		"downloaded_bytes":    pass.DownloadedBytes,
		"duration_seconds":    duration,
		"error":               pass.Error,
		"failures":            len(snapshot.Retries),
		"modules_per_minute":  modulesPerMinute,
		"processed":           pass.Processed,
		"records":             pass.Records,
		"retry_pending":       len(snapshot.Retries),
		"snapshot_generation": snapshot.Generation,
		"status":              status,
		"unavailable":         len(snapshot.Unavailable),
		"workers":             pass.Workers,
	}
	report := map[string]any{
		"coverage_kind":       coverage,
		"finished_at":         pass.FinishedAt.UTC().Format(time.RFC3339Nano),
		"interrupted":         pass.Interrupted,
		"snapshot_generation": snapshot.Generation,
		"sources":             map[string]any{"go": sourceReport},
		"started_at":          pass.StartedAt.UTC().Format(time.RFC3339Nano),
		"status":              status,
	}
	reportBody, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return err
	}
	reportBody = append(reportBody, '\n')

	// The database is canonical. Write observations first and the report last so a
	// report generation is evidence that both compatibility views were attempted.
	if err := atomicWriteStream(paths.Observations, 0o644, func(writer io.Writer) error {
		buffered := bufio.NewWriterSize(writer, 256*1024)
		encoder := json.NewEncoder(buffered)
		encoder.SetEscapeHTML(false)
		if err := observations(func(observation Observation) error {
			return encoder.Encode(observation)
		}); err != nil {
			return err
		}
		return buffered.Flush()
	}); err != nil {
		return err
	}
	if err := atomicWrite(paths.State, stateBody, 0o644); err != nil {
		return err
	}
	return atomicWrite(paths.Report, reportBody, 0o644)
}

func atomicWrite(path string, body []byte, mode os.FileMode) error {
	return atomicWriteStream(path, mode, func(writer io.Writer) error {
		_, err := writer.Write(body)
		return err
	})
}

func atomicWriteStream(path string, mode os.FileMode, write func(io.Writer) error) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(mode); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := write(temporary); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}
