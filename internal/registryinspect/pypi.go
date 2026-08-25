package registryinspect

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

type PyPIInspector struct {
	baseURL string
	http    *requester
}

type pypiCandidate struct {
	PackageType string `json:"packagetype"`
	Filename    string `json:"filename"`
	Size        int64  `json:"size"`
	URL         string `json:"url"`
}

func NewPyPIInspector(config Config) *PyPIInspector {
	if config.BaseURL == "" {
		config.BaseURL = "https://pypi.org"
	}
	return &PyPIInspector{baseURL: strings.TrimRight(config.BaseURL, "/"), http: newRequester(config)}
}

func (i *PyPIInspector) Metrics() Metrics { return i.http.metricsSnapshot() }

func (i *PyPIInspector) Inspect(ctx context.Context, work gocrawl.ModuleWork) gocrawl.ModuleResult {
	result := gocrawl.ModuleResult{Work: work}
	packageCtx, cancel := context.WithTimeout(ctx, i.http.config.PackageTimeout)
	defer cancel()
	metadataURL := i.baseURL + "/pypi/" + url.PathEscape(work.Module) + "/json"
	response, err := i.http.request(packageCtx, http.MethodGet, metadataURL, nil, i.http.config.MaxMetadataBytes)
	if err != nil {
		return classifyResult(ctx, result, err)
	}
	result.DownloadedBytes = int64(len(response.body))
	var payload struct {
		Info struct {
			Name     string `json:"name"`
			Version  string `json:"version"`
			Homepage string `json:"home_page"`
		} `json:"info"`
		URLs []pypiCandidate `json:"urls"`
	}
	if err := json.Unmarshal(response.body, &payload); err != nil {
		return classifyResult(ctx, result, err)
	}
	candidates := make([]pypiCandidate, 0, len(payload.URLs))
	for _, candidate := range payload.URLs {
		if candidate.PackageType == "bdist_wheel" || candidate.PackageType == "sdist" {
			candidates = append(candidates, candidate)
		}
	}
	sort.Slice(candidates, func(left, right int) bool {
		leftWheel, rightWheel := strings.HasSuffix(candidates[left].Filename, ".whl"), strings.HasSuffix(candidates[right].Filename, ".whl")
		if leftWheel != rightWheel {
			return leftWheel
		}
		leftAny, rightAny := strings.Contains(candidates[left].Filename, "none-any"), strings.Contains(candidates[right].Filename, "none-any")
		if leftAny != rightAny {
			return leftAny
		}
		return candidates[left].Filename < candidates[right].Filename
	})
	if len(candidates) == 0 {
		return classifyResult(ctx, result, permanentError{errors.New("latest release has no wheel or sdist")})
	}
	packageName := payload.Info.Name
	if packageName == "" {
		packageName = work.Module
	}
	var repository *string
	if payload.Info.Homepage != "" {
		repository = &payload.Info.Homepage
	}
	var lastErr error
	for _, candidate := range candidates {
		var commands []string
		var downloaded int64
		if strings.HasSuffix(candidate.Filename, ".whl") {
			commands, downloaded, err = i.wheelCommands(packageCtx, candidate.URL, candidate.Size)
		} else {
			commands, downloaded, err = i.sdistCommands(packageCtx, candidate.URL)
		}
		result.DownloadedBytes += downloaded
		if err != nil {
			lastErr = err
			continue
		}
		for _, command := range commands {
			result.Observations = append(result.Observations, gocrawl.Observation{
				Command: command, Confidence: "direct", Ecosystem: "pypi", Language: "python",
				LatestVersion: payload.Info.Version, Package: packageName, Registry: "pypi", Repository: repository,
				Source: candidate.URL, SourceType: "language_package", Version: payload.Info.Version,
			})
		}
		result.Verdict = gocrawl.VerdictSuccess
		return result
	}
	return classifyResult(ctx, result, lastErr)
}

func classifyResult(parent context.Context, result gocrawl.ModuleResult, err error) gocrawl.ModuleResult {
	verdict, message, uncountedRetry := classify(parent, err)
	result.Verdict, result.Error, result.UncountedRetry = gocrawl.Verdict(verdict), message, uncountedRetry
	return result
}

func (i *PyPIInspector) wheelCommands(ctx context.Context, target string, size int64) ([]string, int64, error) {
	var err error
	if size <= 0 {
		size, err = i.artifactSize(ctx, target)
		if err != nil {
			return nil, 0, err
		}
	}
	if size > i.http.config.MaxArtifactBytes {
		return nil, 0, fmt.Errorf("artifact size %d exceeds limit %d", size, i.http.config.MaxArtifactBytes)
	}
	reader := newHTTPRangeReaderAt(ctx, i.http, target, size)
	archive, err := zip.NewReader(reader, size)
	if err == nil {
		commands, inspectErr := commandsFromWheel(archive)
		if inspectErr == nil {
			return commands, reader.Downloaded(), nil
		}
		err = inspectErr
	}
	if size > i.http.config.MaxFullDownloadBytes {
		return nil, reader.Downloaded(), err
	}
	response, fullErr := i.http.request(ctx, http.MethodGet, target, nil, i.http.config.MaxFullDownloadBytes)
	if fullErr != nil {
		return nil, reader.Downloaded(), fullErr
	}
	archive, fullErr = zip.NewReader(bytes.NewReader(response.body), int64(len(response.body)))
	if fullErr != nil {
		return nil, reader.Downloaded() + int64(len(response.body)), fullErr
	}
	commands, fullErr := commandsFromWheel(archive)
	return commands, reader.Downloaded() + int64(len(response.body)), fullErr
}

func (i *PyPIInspector) artifactSize(ctx context.Context, target string) (int64, error) {
	response, err := i.http.request(ctx, http.MethodHead, target, nil, 0)
	if err != nil {
		return 0, err
	}
	size, err := strconv.ParseInt(response.header.Get("Content-Length"), 10, 64)
	if err != nil || size <= 0 {
		return 0, fmt.Errorf("artifact has invalid Content-Length: %s", target)
	}
	if size > i.http.config.MaxArtifactBytes {
		return 0, fmt.Errorf("artifact size %d exceeds limit %d", size, i.http.config.MaxArtifactBytes)
	}
	return size, nil
}

func commandsFromWheel(archive *zip.Reader) ([]string, error) {
	set := make(map[string]struct{})
	for _, file := range archive.File {
		if strings.HasSuffix(file.Name, ".dist-info/entry_points.txt") {
			handle, err := file.Open()
			if err != nil {
				return nil, err
			}
			body, readErr := io.ReadAll(io.LimitReader(handle, 2*1024*1024))
			closeErr := handle.Close()
			if readErr != nil {
				return nil, readErr
			}
			if closeErr != nil {
				return nil, closeErr
			}
			for _, command := range consoleScripts(string(body)) {
				set[command] = struct{}{}
			}
		} else if strings.Contains(file.Name, ".data/scripts/") && !strings.HasSuffix(file.Name, "/") {
			if command := declaredCommand(path.Base(file.Name)); command != "" {
				set[command] = struct{}{}
			}
		}
	}
	return sortedKeys(set), nil
}

func consoleScripts(body string) []string {
	set := make(map[string]struct{})
	section := ""
	for _, raw := range strings.Split(body, "\n") {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") {
			section = line
		} else if section == "[console_scripts]" && strings.Contains(line, "=") {
			if command := strings.TrimSpace(strings.SplitN(line, "=", 2)[0]); command != "" {
				set[command] = struct{}{}
			}
		}
	}
	return sortedKeys(set)
}

func (i *PyPIInspector) sdistCommands(ctx context.Context, target string) ([]string, int64, error) {
	response, err := i.http.request(ctx, http.MethodGet, target, nil, i.http.config.MaxArtifactBytes)
	if err != nil {
		return nil, 0, err
	}
	files, err := metadataFiles(response.body)
	if err != nil {
		return nil, int64(len(response.body)), err
	}
	return commandsFromMetadata(files), int64(len(response.body)), nil
}

func metadataFiles(body []byte) (map[string][]byte, error) {
	if archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body))); err == nil {
		files := make(map[string][]byte)
		for _, file := range archive.File {
			if !interestingMetadata(file.Name) {
				continue
			}
			handle, err := file.Open()
			if err != nil {
				return nil, err
			}
			value, err := io.ReadAll(io.LimitReader(handle, 2*1024*1024))
			_ = handle.Close()
			if err != nil {
				return nil, err
			}
			files[file.Name] = value
		}
		return files, nil
	}
	reader := bytes.NewReader(body)
	var tarReader *tar.Reader
	if zipped, err := gzip.NewReader(reader); err == nil {
		defer zipped.Close()
		tarReader = tar.NewReader(zipped)
	} else {
		tarReader = tar.NewReader(bytes.NewReader(body))
	}
	files := make(map[string][]byte)
	for {
		header, err := tarReader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		if header.Typeflag != tar.TypeReg || !interestingMetadata(header.Name) {
			continue
		}
		value, err := io.ReadAll(io.LimitReader(tarReader, 2*1024*1024))
		if err != nil {
			return nil, err
		}
		files[header.Name] = value
	}
	return files, nil
}

func interestingMetadata(name string) bool {
	return strings.HasSuffix(name, ".dist-info/entry_points.txt") ||
		strings.HasSuffix(name, ".egg-info/entry_points.txt") ||
		strings.HasSuffix(name, "pyproject.toml") || strings.HasSuffix(name, "setup.cfg") ||
		strings.HasSuffix(name, "setup.py")
}

func commandsFromMetadata(files map[string][]byte) []string {
	set := make(map[string]struct{})
	for name, body := range files {
		text := string(body)
		switch {
		case strings.HasSuffix(name, "entry_points.txt"):
			for _, command := range consoleScripts(text) {
				set[command] = struct{}{}
			}
		case strings.HasSuffix(name, "pyproject.toml"):
			collectTOMLScripts(text, set)
		case strings.HasSuffix(name, "setup.cfg"):
			collectSetupCFG(text, set)
		case strings.HasSuffix(name, "setup.py"):
			collectSetupPy(text, set)
		}
	}
	return sortedKeys(set)
}

func collectTOMLScripts(body string, set map[string]struct{}) {
	section := ""
	for _, raw := range strings.Split(body, "\n") {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") {
			section = strings.ToLower(line)
			continue
		}
		if section != "[project.scripts]" && section != "[project.entry-points.console_scripts]" && section != "[tool.poetry.scripts]" {
			continue
		}
		if strings.Contains(line, "=") {
			command := strings.Trim(strings.TrimSpace(strings.SplitN(line, "=", 2)[0]), "\"'")
			if command != "" {
				set[command] = struct{}{}
			}
		}
	}
}

func collectSetupCFG(body string, set map[string]struct{}) {
	inEntryPoints := false
	inConsoleScripts := false
	for _, raw := range strings.Split(body, "\n") {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") {
			inEntryPoints = strings.EqualFold(line, "[options.entry_points]")
			inConsoleScripts = false
			continue
		}
		if !inEntryPoints || line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.TrimLeft(raw, " \t") == raw {
			group, remainder, found := strings.Cut(line, "=")
			inConsoleScripts = found && strings.EqualFold(strings.TrimSpace(group), "console_scripts")
			if inConsoleScripts {
				collectScriptDeclaration(remainder, set)
			}
			continue
		}
		if inConsoleScripts {
			collectScriptDeclaration(line, set)
		}
	}
}

func collectScriptDeclaration(line string, set map[string]struct{}) {
	command, _, found := strings.Cut(strings.TrimSpace(line), "=")
	if found && strings.TrimSpace(command) != "" {
		set[strings.TrimSpace(command)] = struct{}{}
	}
}

var setupPyScripts = regexp.MustCompile(`(?s)console_scripts[^\[\(]*[\[\(](.*?)[\]\)]`)

func collectSetupPy(body string, set map[string]struct{}) {
	for _, match := range setupPyScripts.FindAllStringSubmatch(body, -1) {
		scanner := bufio.NewScanner(strings.NewReader(strings.ReplaceAll(match[1], ",", "\n")))
		for scanner.Scan() {
			line := strings.Trim(strings.TrimSpace(scanner.Text()), "\"'")
			if strings.Contains(line, "=") {
				command := strings.TrimSpace(strings.SplitN(line, "=", 2)[0])
				if command != "" {
					set[command] = struct{}{}
				}
			}
		}
	}
}

func sortedKeys(set map[string]struct{}) []string {
	values := make([]string, 0, len(set))
	for value := range set {
		values = append(values, value)
	}
	sort.Strings(values)
	return values
}
