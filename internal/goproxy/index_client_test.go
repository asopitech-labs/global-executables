package goproxy

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestIndexClientRetriesRateLimitAndDecodesPage(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("since") != "2026-08-20T00:00:00Z" || r.URL.Query().Get("limit") != "2" {
			t.Fatalf("query=%s", r.URL.RawQuery)
		}
		if calls.Add(1) == 1 {
			w.Header().Set("Retry-After", "0")
			http.Error(w, "slow down", http.StatusTooManyRequests)
			return
		}
		_, _ = w.Write([]byte(
			`{"Path":"example.com/a","Version":"v1.0.0","Timestamp":"2026-08-21T00:00:00Z"}` + "\n" +
				`{"Path":"example.com/b","Version":"v2.0.0","Timestamp":"2026-08-21T00:00:01Z"}` + "\n",
		))
	}))
	defer server.Close()
	client := NewIndexClient(server.URL, Config{
		Client: server.Client(), MaxAttempts: 2, RequestTimeout: time.Second,
		Sleep: func(context.Context, time.Duration) error { return nil },
	})
	page, err := client.FetchCatalogPage(context.Background(), "2026-08-20T00:00:00Z", 2)
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 || len(page.Entries) != 2 || page.Complete {
		t.Fatalf("calls=%d page=%+v", calls.Load(), page)
	}
	if page.Entries[1].Path != "example.com/b" || page.Entries[1].Timestamp != "2026-08-21T00:00:01Z" {
		t.Fatalf("entries=%+v", page.Entries)
	}
}
