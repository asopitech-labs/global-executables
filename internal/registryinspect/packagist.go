package registryinspect

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

type PackagistInspector struct {
	baseURL string
	http    *requester
}

func NewPackagistInspector(config Config) *PackagistInspector {
	if config.BaseURL == "" {
		config.BaseURL = "https://repo.packagist.org"
	}
	return &PackagistInspector{baseURL: strings.TrimRight(config.BaseURL, "/"), http: newRequester(config)}
}

func (i *PackagistInspector) Metrics() Metrics { return i.http.metricsSnapshot() }

func (i *PackagistInspector) Inspect(ctx context.Context, work gocrawl.ModuleWork) gocrawl.ModuleResult {
	result := gocrawl.ModuleResult{Work: work}
	packageCtx, cancel := context.WithTimeout(ctx, i.http.config.PackageTimeout)
	defer cancel()
	metadataURL := i.baseURL + "/p2/" + strings.ReplaceAll(url.PathEscape(work.Module), "%2F", "/") + ".json"
	response, err := i.http.request(packageCtx, http.MethodGet, metadataURL, nil, i.http.config.MaxMetadataBytes)
	if err != nil {
		return classifyResult(ctx, result, err)
	}
	result.DownloadedBytes = int64(len(response.body))
	var payload struct {
		Packages map[string][]struct {
			Version  string          `json:"version"`
			Bin      json.RawMessage `json:"bin"`
			Homepage string          `json:"homepage"`
			Source   struct {
				URL string `json:"url"`
			} `json:"source"`
		} `json:"packages"`
	}
	if err := json.Unmarshal(response.body, &payload); err != nil {
		return classifyResult(ctx, result, err)
	}
	versions := payload.Packages[work.Module]
	if len(versions) == 0 {
		result.Verdict = gocrawl.VerdictSuccess
		return result
	}
	latest := versions[0]
	version := latest.Version
	if version == "" {
		version = "unknown"
	}
	var bins []string
	var single string
	if json.Unmarshal(latest.Bin, &single) == nil && single != "" {
		bins = []string{single}
	} else {
		_ = json.Unmarshal(latest.Bin, &bins)
	}
	set := make(map[string]struct{}, len(bins))
	for _, bin := range bins {
		if command := declaredCommand(bin); command != "" {
			set[command] = struct{}{}
		}
	}
	commands := sortedKeys(set)
	var repository *string
	if latest.Source.URL != "" {
		repository = &latest.Source.URL
	} else if latest.Homepage != "" {
		repository = &latest.Homepage
	}
	for _, command := range commands {
		result.Observations = append(result.Observations, gocrawl.Observation{
			Command: command, Confidence: "direct", Ecosystem: "packagist", Language: "php",
			LatestVersion: version, Package: work.Module, Registry: "packagist", Repository: repository,
			Source: metadataURL, SourceType: "language_package", Version: version,
		})
	}
	result.Verdict = gocrawl.VerdictSuccess
	return result
}
