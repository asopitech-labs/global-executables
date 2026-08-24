package goproxy

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

type IndexClient struct {
	endpoint  string
	transport *Inspector
}

func NewIndexClient(endpoint string, config Config) *IndexClient {
	if endpoint == "" {
		endpoint = "https://index.golang.org/index"
	}
	config.BaseURL = endpoint
	return &IndexClient{endpoint: strings.TrimRight(endpoint, "/"), transport: NewInspector(config)}
}

func (c *IndexClient) FetchCatalogPage(
	ctx context.Context,
	since string,
	limit int,
) (gocrawl.CatalogPage, error) {
	if limit <= 0 || limit > 2000 {
		limit = 2000
	}
	endpoint, err := url.Parse(c.endpoint)
	if err != nil {
		return gocrawl.CatalogPage{}, err
	}
	query := endpoint.Query()
	if since != "" {
		query.Set("since", since)
	}
	query.Set("limit", strconv.Itoa(limit))
	endpoint.RawQuery = query.Encode()
	response, err := c.transport.request(ctx, http.MethodGet, endpoint.String(), nil, 8*1024*1024)
	if err != nil {
		return gocrawl.CatalogPage{}, err
	}
	page := gocrawl.CatalogPage{
		Entries: make([]gocrawl.CatalogEntry, 0, limit), DownloadedBytes: uint64(len(response.Body)),
	}
	scanner := bufio.NewScanner(bytes.NewReader(response.Body))
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	var previous time.Time
	for scanner.Scan() {
		if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
			continue
		}
		var entry struct {
			Path      string
			Version   string
			Timestamp string
		}
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			return gocrawl.CatalogPage{}, fmt.Errorf("decode module index entry: %w", err)
		}
		if entry.Path == "" || entry.Timestamp == "" {
			return gocrawl.CatalogPage{}, fmt.Errorf("module index entry lacks path or timestamp")
		}
		timestamp, err := time.Parse(time.RFC3339Nano, entry.Timestamp)
		if err != nil {
			return gocrawl.CatalogPage{}, fmt.Errorf("invalid module index timestamp %q: %w", entry.Timestamp, err)
		}
		if !previous.IsZero() && timestamp.Before(previous) {
			return gocrawl.CatalogPage{}, fmt.Errorf("module index timestamps regressed")
		}
		previous = timestamp
		page.Entries = append(page.Entries, gocrawl.CatalogEntry{
			Path: entry.Path, Version: entry.Version, Timestamp: entry.Timestamp,
		})
	}
	if err := scanner.Err(); err != nil {
		return gocrawl.CatalogPage{}, err
	}
	page.Complete = len(page.Entries) < limit
	return page, nil
}
