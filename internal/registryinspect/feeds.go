package registryinspect

import (
	"bytes"
	"context"
	"encoding/json"
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
	agent   string
}

func newFeedClient(config Config) feedClient {
	timeout := config.RequestTimeout
	if timeout <= 0 {
		timeout = 45 * time.Second
	}
	return feedClient{http: &http.Client{}, timeout: timeout, agent: agent(config.UserAgent)}
}

func (c feedClient) do(ctx context.Context, method, target string, body []byte, contentType string) (int, []byte, error) {
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, target, bytes.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("User-Agent", c.agent)
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
