package goproxy

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"go/parser"
	"go/token"
	"io"
	"net/http"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
	"golang.org/x/mod/module"
	"golang.org/x/mod/semver"
)

type Config struct {
	BaseURL               string
	Client                *http.Client
	RequestTimeout        time.Duration
	ModuleTimeout         time.Duration
	MaxAttempts           int
	BaseBackoff           time.Duration
	MaxBackoff            time.Duration
	FullDownloadThreshold int64
	RangeBlockSize        int64
	RangeCacheBlocks      int
	MaxArchiveBytes       int64
	MaxFullDownloadBytes  int64
	MaxDirectoryProbes    int
	MaxSourceBytes        int64
	Sleep                 func(context.Context, time.Duration) error
}

type Inspector struct {
	config Config
	client *http.Client
	sleep  func(context.Context, time.Duration) error
}

type permanentError struct{ error }

type archiveView struct {
	archive    *zip.Reader
	rangeRead  *httpRangeReaderAt
	downloaded int64
}

func NewInspector(config Config) *Inspector {
	if config.BaseURL == "" {
		config.BaseURL = "https://proxy.golang.org"
	}
	config.BaseURL = strings.TrimRight(config.BaseURL, "/")
	if config.RequestTimeout <= 0 {
		config.RequestTimeout = 45 * time.Second
	}
	if config.ModuleTimeout <= 0 {
		config.ModuleTimeout = 2 * time.Minute
	}
	if config.MaxAttempts <= 0 {
		config.MaxAttempts = 3
	}
	if config.BaseBackoff <= 0 {
		config.BaseBackoff = 250 * time.Millisecond
	}
	if config.MaxBackoff <= 0 {
		config.MaxBackoff = 10 * time.Second
	}
	if config.FullDownloadThreshold <= 0 {
		config.FullDownloadThreshold = 256 * 1024
	}
	if config.RangeBlockSize <= 0 {
		config.RangeBlockSize = 512 * 1024
	}
	if config.RangeCacheBlocks <= 0 {
		config.RangeCacheBlocks = 8
	}
	if config.MaxArchiveBytes <= 0 {
		config.MaxArchiveBytes = 512 * 1024 * 1024
	}
	if config.MaxFullDownloadBytes <= 0 {
		config.MaxFullDownloadBytes = 8 * 1024 * 1024
	}
	if config.MaxDirectoryProbes <= 0 {
		config.MaxDirectoryProbes = 512
	}
	if config.MaxSourceBytes <= 0 {
		config.MaxSourceBytes = 2 * 1024 * 1024
	}
	client := config.Client
	if client == nil {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.MaxIdleConns = 256
		transport.MaxIdleConnsPerHost = 128
		transport.IdleConnTimeout = 90 * time.Second
		transport.ForceAttemptHTTP2 = true
		client = &http.Client{Transport: transport}
	}
	sleep := config.Sleep
	if sleep == nil {
		sleep = defaultSleep
	}
	return &Inspector{config: config, client: client, sleep: sleep}
}

func (i *Inspector) Inspect(ctx context.Context, work gocrawl.ModuleWork) gocrawl.ModuleResult {
	result := gocrawl.ModuleResult{Work: work}
	moduleCtx, cancel := context.WithTimeout(ctx, i.config.ModuleTimeout)
	defer cancel()
	version, err := i.latestVersion(moduleCtx, work.Module)
	if err != nil {
		result.Verdict, result.Error = classifyInspectionError(ctx, err)
		return result
	}
	escapedPath, err := module.EscapePath(work.Module)
	if err != nil {
		result.Verdict, result.Error = gocrawl.VerdictPermanent, err.Error()
		return result
	}
	escapedVersion, err := module.EscapeVersion(version)
	if err != nil {
		result.Verdict, result.Error = gocrawl.VerdictPermanent, err.Error()
		return result
	}
	url := fmt.Sprintf("%s/%s/@v/%s.zip", i.config.BaseURL, escapedPath, escapedVersion)
	observations, downloaded, err := i.inspectArchive(moduleCtx, url, work.Module, version, escapedPath, escapedVersion)
	result.DownloadedBytes = downloaded
	if err != nil {
		result.Verdict, result.Error = classifyInspectionError(ctx, err)
		return result
	}
	result.Verdict = gocrawl.VerdictSuccess
	result.Observations = observations
	return result
}

func classifyInspectionError(parent context.Context, err error) (gocrawl.Verdict, string) {
	if parentErr := parent.Err(); parentErr != nil {
		return gocrawl.VerdictCanceled, parentErr.Error()
	}
	return classify(err)
}

func classify(err error) (gocrawl.Verdict, string) {
	if err == nil {
		return gocrawl.VerdictSuccess, ""
	}
	if errors.Is(err, context.Canceled) {
		return gocrawl.VerdictCanceled, err.Error()
	}
	var permanent permanentError
	if errors.As(err, &permanent) {
		return gocrawl.VerdictPermanent, err.Error()
	}
	var httpErr *HTTPError
	if errors.As(err, &httpErr) {
		switch httpErr.StatusCode {
		case http.StatusNotFound, http.StatusMethodNotAllowed, http.StatusGone, http.StatusUnavailableForLegalReasons:
			return gocrawl.VerdictPermanent, err.Error()
		}
	}
	return gocrawl.VerdictRetry, err.Error()
}

func (i *Inspector) latestVersion(ctx context.Context, modulePath string) (string, error) {
	escaped, err := module.EscapePath(modulePath)
	if err != nil {
		return "", permanentError{err}
	}
	latestURL := fmt.Sprintf("%s/%s/@latest", i.config.BaseURL, escaped)
	response, err := i.request(ctx, http.MethodGet, latestURL, nil, 1024*1024)
	if err == nil {
		var payload struct{ Version string }
		if err := json.Unmarshal(response.Body, &payload); err != nil {
			return "", err
		}
		if payload.Version == "" {
			return "", permanentError{fmt.Errorf("module has no latest version: %s", modulePath)}
		}
		return payload.Version, nil
	}
	var httpErr *HTTPError
	if !errors.As(err, &httpErr) || httpErr.StatusCode != http.StatusNotFound {
		return "", err
	}
	listURL := fmt.Sprintf("%s/%s/@v/list", i.config.BaseURL, escaped)
	list, listErr := i.request(ctx, http.MethodGet, listURL, nil, 4*1024*1024)
	if listErr != nil {
		return "", listErr
	}
	var selected string
	for _, candidate := range strings.Fields(string(list.Body)) {
		if semver.IsValid(candidate) && (selected == "" || semver.Compare(candidate, selected) > 0) {
			selected = candidate
		}
	}
	if selected == "" {
		return "", permanentError{fmt.Errorf("module has no versions: %s", modulePath)}
	}
	return selected, nil
}

func (i *Inspector) inspectArchive(
	ctx context.Context,
	url, modulePath, version, escapedPath, escapedVersion string,
) ([]gocrawl.Observation, int64, error) {
	size, err := i.archiveSize(ctx, url)
	if err != nil {
		return nil, 0, err
	}
	view, err := i.openArchive(ctx, url, size, size <= i.config.FullDownloadThreshold)
	if err != nil {
		return nil, view.bytesDownloaded(), err
	}
	directories := commandCandidates(view.archive, modulePath, version, escapedPath, escapedVersion)
	var probeBytes uint64
	for _, file := range directories {
		probeBytes += file.CompressedSize64
	}
	if view.rangeRead != nil && size <= i.config.MaxFullDownloadBytes &&
		(len(directories) > i.config.MaxDirectoryProbes || probeBytes*2 >= uint64(size)) {
		probeDownloaded := view.bytesDownloaded()
		view, err = i.openArchive(ctx, url, size, true)
		view.downloaded += probeDownloaded
		if err != nil {
			return nil, view.bytesDownloaded(), err
		}
		directories = commandCandidates(view.archive, modulePath, version, escapedPath, escapedVersion)
	}

	commandSet := make(map[string]struct{})
	for directory, file := range directories {
		isMain, err := i.isMainPackage(file)
		if err != nil {
			return nil, view.bytesDownloaded(), err
		}
		if isMain {
			command := path.Base(directory)
			if directory == "" || directory == "." {
				command = path.Base(modulePath)
			}
			commandSet[command] = struct{}{}
		}
	}
	commands := make([]string, 0, len(commandSet))
	for command := range commandSet {
		commands = append(commands, command)
	}
	sort.Strings(commands)
	observations := make([]gocrawl.Observation, 0, len(commands))
	for _, command := range commands {
		observations = append(observations, gocrawl.Observation{
			Command: command, Confidence: "direct", Ecosystem: "go", Language: "go",
			LatestVersion: version, Package: modulePath, Registry: "go", Repository: nil,
			Source: url, SourceType: "language_package", Version: version,
		})
	}
	return observations, view.bytesDownloaded(), nil
}

func (i *Inspector) archiveSize(ctx context.Context, url string) (int64, error) {
	response, err := i.request(ctx, http.MethodHead, url, nil, 0)
	if err == nil {
		if size, parseErr := strconv.ParseInt(response.Header.Get("Content-Length"), 10, 64); parseErr == nil && size > 0 {
			return i.validateArchiveSize(size)
		}
	}
	var httpErr *HTTPError
	if err != nil && (!errors.As(err, &httpErr) || httpErr.StatusCode != http.StatusMethodNotAllowed) {
		return 0, err
	}
	headers := make(http.Header)
	headers.Set("Range", "bytes=0-0")
	probe, err := i.request(ctx, http.MethodGet, url, headers, i.config.MaxFullDownloadBytes)
	if err != nil {
		return 0, err
	}
	if probe.StatusCode == http.StatusOK {
		return i.validateArchiveSize(int64(len(probe.Body)))
	}
	contentRange := probe.Header.Get("Content-Range")
	slash := strings.LastIndexByte(contentRange, '/')
	if slash < 0 {
		return 0, fmt.Errorf("missing archive size in Content-Range %q", contentRange)
	}
	size, err := strconv.ParseInt(contentRange[slash+1:], 10, 64)
	if err != nil || size <= 0 {
		return 0, fmt.Errorf("invalid archive size in Content-Range %q", contentRange)
	}
	return i.validateArchiveSize(size)
}

func (i *Inspector) validateArchiveSize(size int64) (int64, error) {
	if size > i.config.MaxArchiveBytes {
		return 0, fmt.Errorf("archive size %d exceeds limit %d", size, i.config.MaxArchiveBytes)
	}
	return size, nil
}

func (i *Inspector) openArchive(ctx context.Context, url string, size int64, full bool) (archiveView, error) {
	if full {
		if size > i.config.MaxFullDownloadBytes {
			return archiveView{}, fmt.Errorf("archive size %d exceeds full-download limit %d", size, i.config.MaxFullDownloadBytes)
		}
		response, err := i.request(ctx, http.MethodGet, url, nil, i.config.MaxFullDownloadBytes)
		if err != nil {
			return archiveView{}, err
		}
		archive, err := zip.NewReader(bytes.NewReader(response.Body), int64(len(response.Body)))
		return archiveView{archive: archive, downloaded: int64(len(response.Body))}, err
	}
	rangeRead := newHTTPRangeReaderAt(ctx, i, url, size, i.config.RangeBlockSize, i.config.RangeCacheBlocks)
	archive, err := zip.NewReader(rangeRead, size)
	return archiveView{archive: archive, rangeRead: rangeRead}, err
}

func (v archiveView) bytesDownloaded() int64 {
	if v.rangeRead != nil {
		return v.rangeRead.Downloaded()
	}
	return v.downloaded
}

func commandCandidates(archive *zip.Reader, modulePath, version, escapedPath, escapedVersion string) map[string]*zip.File {
	actualRoot := modulePath + "@" + version + "/"
	escapedRoot := escapedPath + "@" + escapedVersion + "/"
	directories := make(map[string]*zip.File)
	for _, file := range archive.File {
		name := file.Name
		var relative string
		switch {
		case strings.HasPrefix(name, actualRoot):
			relative = strings.TrimPrefix(name, actualRoot)
		case strings.HasPrefix(name, escapedRoot):
			relative = strings.TrimPrefix(name, escapedRoot)
		default:
			continue
		}
		if !strings.HasSuffix(relative, ".go") || strings.HasSuffix(relative, "_test.go") || excludedPath(relative) {
			continue
		}
		directory := path.Dir(relative)
		if directory == "." {
			directory = ""
		}
		if _, exists := directories[directory]; !exists {
			directories[directory] = file
		}
	}
	return directories
}

func excludedPath(relative string) bool {
	for _, part := range strings.Split(relative, "/") {
		if part == "vendor" || part == "testdata" {
			return true
		}
	}
	return false
}

func (i *Inspector) isMainPackage(file *zip.File) (bool, error) {
	reader, err := file.Open()
	if err != nil {
		return false, err
	}
	body, readErr := io.ReadAll(io.LimitReader(reader, i.config.MaxSourceBytes))
	closeErr := reader.Close()
	if readErr != nil {
		return false, readErr
	}
	if closeErr != nil {
		return false, closeErr
	}
	syntax, err := parser.ParseFile(token.NewFileSet(), file.Name, body, parser.PackageClauseOnly)
	if err != nil {
		return false, err
	}
	return syntax.Name.Name == "main", nil
}
