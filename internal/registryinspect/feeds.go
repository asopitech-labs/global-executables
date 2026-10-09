package registryinspect

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

const (
	// pypiMaxSerialGap is the largest number of PyPI changelog entries one poll replays
	// (about a week of traffic). A longer gap is reported as a resynchronisation
	// instead, and the rotation re-checks everything.
	pypiMaxSerialGap = 300_000
	// npmChangesPage and npmMaxPages bound one poll of the npm replication feed.
	npmChangesPage = 5000
	npmMaxPages    = 40
	feedBodyLimit  = 256 << 20
)

// feedClient is a small HTTP client for change feeds. A feed poll that fails is not
// retried inside the run: the cursor stays where it was and the next run replays.
type feedClient struct {
	http    *http.Client
	timeout time.Duration
}

func newFeedClient(config Config) feedClient {
	timeout := config.RequestTimeout
	if timeout <= 0 {
		timeout = 45 * time.Second
	}
	return feedClient{http: &http.Client{}, timeout: timeout}
}

func (c feedClient) do(ctx context.Context, method, target string, body []byte, contentType string) (int, []byte, error) {
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, target, bytes.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept-Encoding", "identity")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, feedBodyLimit))
	return resp.StatusCode, data, err
}

// PyPIFeed reads the PyPI XML-RPC changelog. The cursor is a serial number.
type PyPIFeed struct {
	baseURL string
	client  feedClient
}

func NewPyPIFeed(config Config) *PyPIFeed {
	if config.BaseURL == "" {
		config.BaseURL = "https://pypi.org"
	}
	return &PyPIFeed{baseURL: strings.TrimRight(config.BaseURL, "/"), client: newFeedClient(config)}
}

type xmlRPCValue struct {
	String *string `xml:"string"`
	Int    *int64  `xml:"int"`
	I4     *int64  `xml:"i4"`
	Array  *struct {
		Values []xmlRPCValue `xml:"data>value"`
	} `xml:"array"`
}

type xmlRPCResponse struct {
	Value xmlRPCValue `xml:"params>param>value"`
	Fault *struct{}   `xml:"fault"`
}

func (v xmlRPCValue) integer() (int64, bool) {
	switch {
	case v.Int != nil:
		return *v.Int, true
	case v.I4 != nil:
		return *v.I4, true
	}
	return 0, false
}

func xmlRPCCall(method string, serial *int64) []byte {
	var body strings.Builder
	body.WriteString(`<?xml version="1.0"?><methodCall><methodName>` + method + `</methodName>`)
	if serial != nil {
		body.WriteString(`<params><param><value><int>` + strconv.FormatInt(*serial, 10) + `</int></value></param></params>`)
	}
	body.WriteString(`</methodCall>`)
	return []byte(body.String())
}

func (f *PyPIFeed) call(ctx context.Context, method string, serial *int64) (xmlRPCResponse, int64, error) {
	status, data, err := f.client.do(ctx, http.MethodPost, f.baseURL+"/pypi", xmlRPCCall(method, serial), "text/xml")
	if err != nil {
		return xmlRPCResponse{}, 0, err
	}
	if status != http.StatusOK {
		return xmlRPCResponse{}, int64(len(data)), fmt.Errorf("pypi %s: status %d", method, status)
	}
	var response xmlRPCResponse
	if err := xml.Unmarshal(data, &response); err != nil {
		return xmlRPCResponse{}, int64(len(data)), fmt.Errorf("pypi %s: %w", method, err)
	}
	if response.Fault != nil {
		return xmlRPCResponse{}, int64(len(data)), fmt.Errorf("pypi %s: fault", method)
	}
	return response, int64(len(data)), nil
}

// pypiRelevantAction reports whether a changelog action can change which release is
// the latest or what it contains. Ownership and metadata edits cannot.
func pypiRelevantAction(action string) bool {
	switch {
	case strings.Contains(action, "Owner"), strings.Contains(action, "Maintainer"):
		return false
	case strings.HasPrefix(action, "new release"), strings.HasPrefix(action, "create"),
		strings.HasPrefix(action, "remove"), strings.HasPrefix(action, "yank"), strings.HasPrefix(action, "unyank"):
		return true
	case strings.HasPrefix(action, "add ") && strings.Contains(action, " file"):
		return true
	}
	return false
}

func (f *PyPIFeed) Poll(ctx context.Context, cursor string) (gocrawl.FeedPage, error) {
	page := gocrawl.FeedPage{}
	last, bytesRead, err := f.call(ctx, "changelog_last_serial", nil)
	page.Requests++
	page.DownloadedBytes += bytesRead
	if err != nil {
		return page, err
	}
	latest, ok := last.Value.integer()
	if !ok {
		return page, errors.New("pypi changelog_last_serial: no serial in response")
	}
	page.Cursor = strconv.FormatInt(latest, 10)
	if cursor == "" {
		return page, nil
	}
	since, err := strconv.ParseInt(cursor, 10, 64)
	if err != nil {
		return page, fmt.Errorf("pypi feed cursor %q: %w", cursor, err)
	}
	if latest <= since {
		page.Cursor = cursor
		return page, nil
	}
	if latest-since > pypiMaxSerialGap {
		page.Resync = true
		return page, nil
	}
	changes, bytesRead, err := f.call(ctx, "changelog_since_serial", &since)
	page.Requests++
	page.DownloadedBytes += bytesRead
	if err != nil {
		return gocrawl.FeedPage{Requests: page.Requests, DownloadedBytes: page.DownloadedBytes}, err
	}
	if changes.Value.Array == nil {
		return gocrawl.FeedPage{Requests: page.Requests, DownloadedBytes: page.DownloadedBytes}, errors.New("pypi changelog_since_serial: no array in response")
	}
	highest := since
	for _, row := range changes.Value.Array.Values {
		if row.Array == nil || len(row.Array.Values) < 5 {
			continue
		}
		fields := row.Array.Values
		if serial, ok := fields[4].integer(); ok {
			highest = max(highest, serial)
		}
		if fields[0].String == nil || fields[3].String == nil || !pypiRelevantAction(*fields[3].String) {
			continue
		}
		stamp, _ := fields[2].integer()
		page.Events = append(page.Events, gocrawl.FeedEvent{Name: *fields[0].String, Time: stamp, Kind: *fields[3].String})
	}
	// Advance only as far as the rows actually read, not to the serial seen before the
	// second call: a row appended in between is read by the next poll.
	page.Cursor = strconv.FormatInt(highest, 10)
	return page, nil
}

// NPMFeed reads the npm replication _changes feed. The cursor is a sequence number.
type NPMFeed struct {
	baseURL string
	client  feedClient
}

func NewNPMFeed(config Config, replicateURL string) *NPMFeed {
	if replicateURL == "" {
		replicateURL = "https://replicate.npmjs.com"
	}
	return &NPMFeed{baseURL: strings.TrimRight(replicateURL, "/"), client: newFeedClient(config)}
}

func (f *NPMFeed) Poll(ctx context.Context, cursor string) (gocrawl.FeedPage, error) {
	page := gocrawl.FeedPage{}
	if cursor == "" {
		status, data, err := f.client.do(ctx, http.MethodGet, f.baseURL+"/", nil, "")
		page.Requests++
		page.DownloadedBytes += int64(len(data))
		if err != nil {
			return page, err
		}
		if status != http.StatusOK {
			return page, fmt.Errorf("npm replicate: status %d", status)
		}
		var info struct {
			UpdateSeq int64 `json:"update_seq"`
		}
		if err := json.Unmarshal(data, &info); err != nil || info.UpdateSeq <= 0 {
			return page, errors.New("npm replicate: no update_seq in response")
		}
		page.Cursor = strconv.FormatInt(info.UpdateSeq, 10)
		return page, nil
	}
	since := cursor
	page.Cursor = cursor
	for range npmMaxPages {
		target := fmt.Sprintf("%s/_changes?since=%s&limit=%d", f.baseURL, url.QueryEscape(since), npmChangesPage)
		status, data, err := f.client.do(ctx, http.MethodGet, target, nil, "")
		page.Requests++
		page.DownloadedBytes += int64(len(data))
		if err != nil {
			return pageOrError(page, err)
		}
		if status != http.StatusOK {
			return pageOrError(page, fmt.Errorf("npm _changes: status %d", status))
		}
		var changes struct {
			Results []struct {
				ID      string `json:"id"`
				Deleted bool   `json:"deleted"`
			} `json:"results"`
			LastSeq json.Number `json:"last_seq"`
		}
		if err := json.Unmarshal(data, &changes); err != nil || changes.LastSeq == "" {
			return pageOrError(page, errors.New("npm _changes: malformed response"))
		}
		for _, change := range changes.Results {
			if change.ID == "" || strings.HasPrefix(change.ID, "_design/") {
				continue
			}
			kind := "change"
			if change.Deleted {
				kind = "delete"
			}
			page.Events = append(page.Events, gocrawl.FeedEvent{Name: change.ID, Kind: kind})
		}
		since = changes.LastSeq.String()
		page.Cursor = since
		if len(changes.Results) < npmChangesPage {
			return page, nil
		}
	}
	return page, nil
}

// pageOrError keeps what earlier pages of the same poll already read. The cursor in the
// page is the last one fully read, so the committed position matches the events kept.
func pageOrError(page gocrawl.FeedPage, err error) (gocrawl.FeedPage, error) {
	if len(page.Events) > 0 {
		return page, nil
	}
	return page, err
}

// PackagistFeed reads metadata/changes.json. The cursor is the feed's own timestamp.
type PackagistFeed struct {
	baseURL string
	client  feedClient
}

func NewPackagistFeed(config Config, apiURL string) *PackagistFeed {
	if apiURL == "" {
		apiURL = "https://packagist.org"
	}
	return &PackagistFeed{baseURL: strings.TrimRight(apiURL, "/"), client: newFeedClient(config)}
}

func (f *PackagistFeed) Poll(ctx context.Context, cursor string) (gocrawl.FeedPage, error) {
	page := gocrawl.FeedPage{Cursor: cursor}
	target := f.baseURL + "/metadata/changes.json"
	if cursor != "" {
		target += "?since=" + url.QueryEscape(cursor)
	}
	status, data, err := f.client.do(ctx, http.MethodGet, target, nil, "")
	page.Requests++
	page.DownloadedBytes += int64(len(data))
	if err != nil {
		return page, err
	}
	var body struct {
		Actions []struct {
			Type    string `json:"type"`
			Package string `json:"package"`
			Time    int64  `json:"time"`
		} `json:"actions"`
		Timestamp json.Number `json:"timestamp"`
	}
	decodeErr := json.Unmarshal(data, &body)
	if cursor == "" {
		// Without a starting point the endpoint answers 400 and hands out the
		// timestamp to start mirroring from.
		if decodeErr != nil || body.Timestamp == "" {
			return page, fmt.Errorf("packagist changes: status %d without a timestamp", status)
		}
		page.Cursor = body.Timestamp.String()
		return page, nil
	}
	if status != http.StatusOK || decodeErr != nil || body.Timestamp == "" {
		return page, fmt.Errorf("packagist changes: status %d", status)
	}
	page.Cursor = body.Timestamp.String()
	for _, action := range body.Actions {
		if action.Type == "resync" {
			// The feed cannot say what changed: every package becomes due.
			page.Resync = true
			page.Events = nil
			return page, nil
		}
		if strings.HasSuffix(action.Package, "~dev") || action.Package == "" {
			continue
		}
		page.Events = append(page.Events, gocrawl.FeedEvent{Name: action.Package, Time: action.Time, Kind: action.Type})
	}
	return page, nil
}
