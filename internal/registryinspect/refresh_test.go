package registryinspect

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestPyPIInspectorSkipsArtifactsWhenLatestVersionIsRecorded(t *testing.T) {
	var metadata, files atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/pypi/demo/json" {
			metadata.Add(1)
			_, _ = fmt.Fprint(w, `{"info":{"name":"demo","version":"2.0.0"},"urls":[{"packagetype":"sdist","filename":"demo-2.0.0.tar.gz","size":10,"url":"http://`+r.Host+`/files/demo.tar.gz"}]}`)
			return
		}
		files.Add(1)
		http.NotFound(w, r)
	}))
	defer server.Close()
	inspector := NewPyPIInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second})

	result := inspector.Inspect(t.Context(), gocrawl.ModuleWork{Module: "demo", Known: "2.0.0"})
	if result.Verdict != gocrawl.VerdictSuccess || !result.Unchanged || result.Latest != "2.0.0" || len(result.Observations) != 0 {
		t.Fatalf("result=%+v", result)
	}
	if metadata.Load() != 1 || files.Load() != 0 {
		t.Fatalf("metadata=%d artifact requests=%d, want 1 and 0", metadata.Load(), files.Load())
	}
	// A different recorded version is a change: the artifacts are read again.
	result = inspector.Inspect(t.Context(), gocrawl.ModuleWork{Module: "demo", Known: "1.0.0"})
	if result.Unchanged || files.Load() == 0 {
		t.Fatalf("a changed version must be inspected: %+v files=%d", result, files.Load())
	}
}

func TestRubyGemsInspectorSkipsGemDownloadWhenLatestVersionIsRecorded(t *testing.T) {
	var gems atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/gems/demo.json" {
			_, _ = fmt.Fprint(w, `{"name":"demo","version":"1.2.3"}`)
			return
		}
		gems.Add(1)
		http.NotFound(w, r)
	}))
	defer server.Close()
	inspector := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second})
	result := inspector.Inspect(t.Context(), gocrawl.ModuleWork{Module: "demo", Known: "1.2.3"})
	if !result.Unchanged || result.Latest != "1.2.3" || gems.Load() != 0 {
		t.Fatalf("result=%+v gem requests=%d", result, gems.Load())
	}
}

func TestRubyGemsInspectorStillTreatsYankedGemAsPermanentWhenVersionIsRecorded(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = fmt.Fprint(w, `{"name":"demo","version":"1.2.3","yanked":true}`)
	}))
	defer server.Close()
	inspector := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second})
	result := inspector.Inspect(t.Context(), gocrawl.ModuleWork{Module: "demo", Known: "1.2.3"})
	if result.Verdict != gocrawl.VerdictPermanent || result.Unchanged {
		t.Fatalf("a yanked gem must leave the dataset even when its version is recorded: %+v", result)
	}
}

func TestNPMAndPackagistInspectorsReportUnchangedVersions(t *testing.T) {
	npm := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = fmt.Fprint(w, `{"name":"demo","version":"1.0.0","bin":{"demo":"cli.js"}}`)
	}))
	defer npm.Close()
	result := NewNPMInspector(Config{BaseURL: npm.URL, RequestTimeout: time.Second}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "demo", Known: "1.0.0"})
	if !result.Unchanged || result.Latest != "1.0.0" || len(result.Observations) != 0 {
		t.Fatalf("npm result=%+v", result)
	}
	packagist := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = fmt.Fprint(w, `{"packages":{"acme/demo":[{"version":"3.1.0","bin":["bin/demo"]}]}}`)
	}))
	defer packagist.Close()
	result = NewPackagistInspector(Config{BaseURL: packagist.URL, RequestTimeout: time.Second}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "acme/demo", Known: "3.1.0"})
	if !result.Unchanged || result.Latest != "3.1.0" {
		t.Fatalf("packagist result=%+v", result)
	}
	result = NewPackagistInspector(Config{BaseURL: packagist.URL, RequestTimeout: time.Second}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "acme/demo", Known: "3.0.0"})
	if result.Unchanged || len(result.Observations) != 1 || result.Latest != "3.1.0" {
		t.Fatalf("a new version must be extracted: %+v", result)
	}
}

func TestNPMFeedPagesThroughChangesAndMarksDeletes(t *testing.T) {
	var requested []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requested = append(requested, r.URL.RequestURI())
		switch {
		case r.URL.Path == "/":
			_, _ = fmt.Fprint(w, `{"db_name":"registry","update_seq":900}`)
		case r.URL.Query().Get("since") == "100":
			var results strings.Builder
			for index := range npmChangesPage {
				if index > 0 {
					results.WriteByte(',')
				}
				fmt.Fprintf(&results, `{"seq":%d,"id":"pkg-%d","changes":[]}`, 101+index, index)
			}
			fmt.Fprintf(w, `{"results":[%s],"last_seq":%d}`, results.String(), 100+npmChangesPage)
		default:
			_, _ = fmt.Fprint(w, `{"results":[{"seq":9000,"id":"gone","deleted":true,"changes":[]},{"seq":9001,"id":"_design/app","changes":[]}],"last_seq":9001}`)
		}
	}))
	defer server.Close()
	feed := NewNPMFeed(Config{RequestTimeout: time.Second}, server.URL)
	first, err := feed.Poll(t.Context(), "")
	if err != nil || first.Cursor != "900" || len(first.Events) != 0 {
		t.Fatalf("first=%+v err=%v", first, err)
	}
	page, err := feed.Poll(t.Context(), "100")
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Events) != npmChangesPage+1 || page.Cursor != "9001" || page.Requests != 2 {
		t.Fatalf("events=%d cursor=%s requests=%d", len(page.Events), page.Cursor, page.Requests)
	}
	last := page.Events[len(page.Events)-1]
	if last.Name != "gone" || last.Kind != "delete" {
		t.Fatalf("last event=%+v (design documents must be dropped)", last)
	}
}

func TestNPMFeedKeepsPagesReadBeforeAFailure(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			var results strings.Builder
			for index := range npmChangesPage {
				if index > 0 {
					results.WriteByte(',')
				}
				fmt.Fprintf(&results, `{"seq":%d,"id":"p%d","changes":[]}`, index+1, index)
			}
			fmt.Fprintf(w, `{"results":[%s],"last_seq":%d}`, results.String(), npmChangesPage)
			return
		}
		http.Error(w, "boom", http.StatusServiceUnavailable)
	}))
	defer server.Close()
	page, err := NewNPMFeed(Config{RequestTimeout: time.Second}, server.URL).Poll(t.Context(), "0")
	if err != nil || len(page.Events) != npmChangesPage || page.Cursor != fmt.Sprint(npmChangesPage) {
		t.Fatalf("the cursor must match the events kept: cursor=%s events=%d err=%v", page.Cursor, len(page.Events), err)
	}
}

func TestPackagistFeedStartsFromServerTimestampAndHandlesResyncAndDev(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Query().Get("since") {
		case "":
			w.WriteHeader(http.StatusBadRequest)
			_, _ = fmt.Fprint(w, `{"error":"Invalid or missing since","timestamp":17915281730011}`)
		case "17915281730011":
			_, _ = fmt.Fprint(w, `{"actions":[{"type":"update","package":"acme/demo","time":1791528200},{"type":"update","package":"acme/demo~dev","time":1791528201},{"type":"delete","package":"acme/old","time":1791528202}],"timestamp":17915282000011}`)
		default:
			_, _ = fmt.Fprint(w, `{"actions":[{"type":"resync","time":1791528300}],"timestamp":17915283000011}`)
		}
	}))
	defer server.Close()
	feed := NewPackagistFeed(Config{RequestTimeout: time.Second}, server.URL)
	first, err := feed.Poll(t.Context(), "")
	if err != nil || first.Cursor != "17915281730011" {
		t.Fatalf("first=%+v err=%v", first, err)
	}
	page, err := feed.Poll(t.Context(), first.Cursor)
	if err != nil || page.Cursor != "17915282000011" || len(page.Events) != 2 {
		t.Fatalf("page=%+v err=%v", page, err)
	}
	if page.Events[0].Name != "acme/demo" || page.Events[1].Kind != "delete" {
		t.Fatalf("events=%+v (the ~dev entry must be dropped)", page.Events)
	}
	resync, err := feed.Poll(t.Context(), "17915282000011")
	if err != nil || !resync.Resync || len(resync.Events) != 0 || resync.Cursor != "17915283000011" {
		t.Fatalf("resync=%+v err=%v", resync, err)
	}
}
