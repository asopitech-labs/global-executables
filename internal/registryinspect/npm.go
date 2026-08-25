package registryinspect

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"path"
	"sort"
	"strings"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

type NPMInspector struct {
	baseURL string
	http    *requester
}

func NewNPMInspector(config Config) *NPMInspector {
	if config.BaseURL == "" {
		config.BaseURL = "https://registry.npmjs.org"
	}
	return &NPMInspector{baseURL: strings.TrimRight(config.BaseURL, "/"), http: newRequester(config)}
}

func (i *NPMInspector) Metrics() Metrics { return i.http.metricsSnapshot() }

func (i *NPMInspector) Inspect(ctx context.Context, work gocrawl.ModuleWork) gocrawl.ModuleResult {
	result := gocrawl.ModuleResult{Work: work}
	packageCtx, cancel := context.WithTimeout(ctx, i.http.config.PackageTimeout)
	defer cancel()
	escaped := strings.ReplaceAll(url.PathEscape(work.Module), "%40", "@")
	metadataURL := i.baseURL + "/" + escaped + "/latest"
	response, err := i.http.request(packageCtx, http.MethodGet, metadataURL, nil, i.http.config.MaxMetadataBytes)
	if err != nil {
		verdict, message := classify(ctx, err)
		result.Verdict, result.Error = gocrawl.Verdict(verdict), message
		return result
	}
	result.DownloadedBytes = int64(len(response.body))
	var payload struct {
		Name       string          `json:"name"`
		Version    string          `json:"version"`
		Bin        json.RawMessage `json:"bin"`
		Repository json.RawMessage `json:"repository"`
	}
	if err := json.Unmarshal(response.body, &payload); err != nil {
		result.Verdict, result.Error = gocrawl.VerdictRetry, err.Error()
		return result
	}
	if payload.Name == "" {
		payload.Name = work.Module
	}
	commands, err := npmCommands(payload.Name, payload.Bin)
	if err != nil {
		result.Verdict, result.Error = gocrawl.VerdictRetry, err.Error()
		return result
	}
	repository := npmRepository(payload.Repository)
	for _, command := range commands {
		result.Observations = append(result.Observations, gocrawl.Observation{
			Command: command, Confidence: "direct", Ecosystem: "npm", Language: "javascript",
			LatestVersion: payload.Version, Package: payload.Name, Registry: "npm", Repository: repository,
			Source: metadataURL, SourceType: "language_package", Version: payload.Version,
		})
	}
	result.Verdict = gocrawl.VerdictSuccess
	return result
}

func npmCommands(packageName string, raw json.RawMessage) ([]string, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var single string
	if err := json.Unmarshal(raw, &single); err == nil {
		return []string{path.Base(packageName)}, nil
	}
	var bins map[string]json.RawMessage
	if err := json.Unmarshal(raw, &bins); err != nil {
		return nil, err
	}
	set := make(map[string]struct{}, len(bins))
	for name := range bins {
		command := declaredCommand(name)
		if command != "" {
			set[command] = struct{}{}
		}
	}
	commands := make([]string, 0, len(set))
	for command := range set {
		commands = append(commands, command)
	}
	sort.Strings(commands)
	return commands, nil
}

func npmRepository(raw json.RawMessage) *string {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var value string
	if json.Unmarshal(raw, &value) == nil && value != "" {
		return &value
	}
	var object struct {
		URL string `json:"url"`
	}
	if json.Unmarshal(raw, &object) == nil && object.URL != "" {
		return &object.URL
	}
	return nil
}

func declaredCommand(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	value = path.Base(value)
	if value == "." || value == ".." || value == "/" {
		return ""
	}
	return value
}
