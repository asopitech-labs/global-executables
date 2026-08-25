package registryinspect

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

const gemHeadBytes int64 = 65536

type RubyGemsInspector struct {
	baseURL string
	http    *requester
}

func NewRubyGemsInspector(config Config) *RubyGemsInspector {
	if config.BaseURL == "" {
		config.BaseURL = "https://rubygems.org"
	}
	return &RubyGemsInspector{baseURL: strings.TrimRight(config.BaseURL, "/"), http: newRequester(config)}
}

func (i *RubyGemsInspector) Metrics() Metrics { return i.http.metricsSnapshot() }

func (i *RubyGemsInspector) Inspect(ctx context.Context, work gocrawl.ModuleWork) gocrawl.ModuleResult {
	result := gocrawl.ModuleResult{Work: work}
	packageCtx, cancel := context.WithTimeout(ctx, i.http.config.PackageTimeout)
	defer cancel()
	escaped := url.PathEscape(work.Module)
	metadataURL := i.baseURL + "/api/v1/gems/" + escaped + ".json"
	response, err := i.http.request(packageCtx, http.MethodGet, metadataURL, nil, i.http.config.MaxMetadataBytes)
	if err != nil {
		return classifyResult(ctx, result, err)
	}
	var metadata struct {
		Name          string  `json:"name"`
		Version       string  `json:"version"`
		Yanked        bool    `json:"yanked"`
		GemURI        string  `json:"gem_uri"`
		SourceCodeURI *string `json:"source_code_uri"`
		HomepageURI   *string `json:"homepage_uri"`
	}
	if err := json.Unmarshal(response.body, &metadata); err != nil {
		return classifyResult(ctx, result, err)
	}
	if metadata.Yanked {
		return classifyResult(ctx, result, permanentError{errors.New("gem is yanked")})
	}
	if metadata.Name == "" {
		metadata.Name = work.Module
	}
	if metadata.Version == "" {
		metadata.Version = "unknown"
	}
	artifactURL := metadata.GemURI
	if artifactURL == "" {
		artifactURL = i.baseURL + "/gems/" + escaped + "-" + url.PathEscape(metadata.Version) + ".gem"
	}
	gemspec, downloaded, err := i.gemspec(packageCtx, artifactURL)
	result.DownloadedBytes += downloaded
	if err != nil {
		return classifyResult(ctx, result, err)
	}
	var repository *string
	if metadata.SourceCodeURI != nil && *metadata.SourceCodeURI != "" {
		repository = metadata.SourceCodeURI
	} else {
		repository = metadata.HomepageURI
	}
	commands, err := gemspecCommands(string(gemspec))
	if err != nil {
		return classifyResult(ctx, result, err)
	}
	for _, command := range commands {
		result.Observations = append(result.Observations, gocrawl.Observation{
			Command: command, Confidence: "direct", Ecosystem: "rubygems", Language: "ruby",
			LatestVersion: metadata.Version, Package: metadata.Name, Registry: "rubygems", Repository: repository,
			Source: artifactURL, SourceType: "language_package", Version: metadata.Version,
		})
	}
	result.Verdict = gocrawl.VerdictSuccess
	return result
}

func (i *RubyGemsInspector) gemspec(ctx context.Context, target string) ([]byte, int64, error) {
	headers := make(http.Header)
	headers.Set("Range", fmt.Sprintf("bytes=0-%d", gemHeadBytes-1))
	head, headErr := i.http.request(ctx, http.MethodGet, target, headers, gemHeadBytes)
	if headErr == nil {
		if metadata, err := gemMetadata(head.body, i.http.config.MaxMetadataBytes); err == nil {
			return metadata, int64(len(head.body)), nil
		}
	}
	full, err := i.http.request(ctx, http.MethodGet, target, nil, i.http.config.MaxArtifactBytes)
	if err != nil {
		return nil, 0, err
	}
	metadata, err := gemMetadata(full.body, i.http.config.MaxMetadataBytes)
	return metadata, int64(len(full.body)), err
}

func gemMetadata(body []byte, limit int64) ([]byte, error) {
	archive := tar.NewReader(bytes.NewReader(body))
	for {
		header, err := archive.Next()
		if errors.Is(err, io.EOF) {
			return nil, errors.New("gem archive has no metadata.gz")
		}
		if err != nil {
			return nil, err
		}
		if header.Name != "metadata.gz" {
			continue
		}
		compressed, err := io.ReadAll(io.LimitReader(archive, limit+1))
		if err != nil {
			return nil, err
		}
		reader, err := gzip.NewReader(bytes.NewReader(compressed))
		if err != nil {
			return nil, err
		}
		metadata, readErr := io.ReadAll(io.LimitReader(reader, limit+1))
		closeErr := reader.Close()
		if readErr != nil {
			return nil, readErr
		}
		if closeErr != nil {
			return nil, closeErr
		}
		if int64(len(metadata)) > limit {
			return nil, fmt.Errorf("gem metadata exceeded %d bytes", limit)
		}
		return metadata, nil
	}
}

func gemspecCommands(body string) ([]string, error) {
	set := make(map[string]struct{})
	lines := strings.Split(body, "\n")
	for index, raw := range lines {
		line := strings.TrimSpace(raw)
		if !strings.HasPrefix(line, "executables:") {
			continue
		}
		remainder := strings.TrimSpace(strings.TrimPrefix(line, "executables:"))
		if remainder != "" && (!strings.HasPrefix(remainder, "[") || !strings.HasSuffix(remainder, "]")) {
			return nil, errors.New("malformed executables list")
		}
		if remainder != "" && remainder != "[]" {
			for value := range strings.SplitSeq(strings.Trim(remainder, "[]"), ",") {
				if command := declaredCommand(strings.Trim(value, " \\\"'")); command != "" {
					set[command] = struct{}{}
				}
			}
		}
		for _, child := range lines[index+1:] {
			trimmed := strings.TrimSpace(child)
			if strings.HasPrefix(trimmed, "-") {
				if command := declaredCommand(strings.Trim(strings.TrimSpace(strings.TrimPrefix(trimmed, "-")), "\\\"'")); command != "" {
					set[command] = struct{}{}
				}
			} else if trimmed != "" && !strings.HasPrefix(child, " ") && !strings.HasPrefix(child, "\t") {
				break
			}
		}
		break
	}
	return sortedKeys(set), nil
}
