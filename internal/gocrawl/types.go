package gocrawl

import "context"

type Verdict string

const (
	VerdictSuccess   Verdict = "success"
	VerdictRetry     Verdict = "retry"
	VerdictPermanent Verdict = "permanent"
	VerdictCanceled  Verdict = "canceled"
)

type ModuleWork struct {
	Order         uint64
	CatalogIndex  uint64
	CatalogOffset int64
	Module        string
	Retry         bool
	Refresh       bool
	Attempt       int
	// Feed marks work that a registry change feed announced. It does not move the
	// catalog cursors, like Retry.
	Feed bool
	// Known is the latest version recorded by the last successful check, or empty.
	// An inspector that finds the same version may report the result as unchanged
	// instead of reading artifacts again.
	Known string
	// Skip marks a rotation entry that is not due yet: it advances the cursor and
	// nothing else.
	Skip bool
}

type Observation struct {
	Command       string  `json:"command"`
	Confidence    string  `json:"confidence"`
	Ecosystem     string  `json:"ecosystem"`
	Language      string  `json:"language"`
	LatestVersion string  `json:"latest_version"`
	Package       string  `json:"package"`
	Registry      string  `json:"registry"`
	Repository    *string `json:"repository"`
	Source        string  `json:"source"`
	SourceType    string  `json:"source_type"`
	Version       string  `json:"version"`
}

type ModuleResult struct {
	Work            ModuleWork
	Verdict         Verdict
	Observations    []Observation
	Error           string
	DownloadedBytes int64
	UncountedRetry  bool
	// Latest is the latest version the registry reported, recorded in the check.
	Latest string
	// Unchanged means Latest equals Work.Known: the stored observations stay as they
	// are and only the check advances.
	Unchanged bool
}

// AsUnchanged turns result into a successful look that found nothing new.
func (r ModuleResult) AsUnchanged(latest string) ModuleResult {
	r.Verdict, r.Latest, r.Unchanged = VerdictSuccess, latest, true
	r.Observations = nil
	return r
}

// SameVersion reports whether latest is the version the previous check recorded.
func (w ModuleWork) SameVersion(latest string) bool {
	return w.Known != "" && latest != "" && w.Known == latest
}

// FeedEntry is one change a feed announced for a module.
type FeedEntry struct {
	Time int64  `json:"time,omitempty"`
	Kind string `json:"kind,omitempty"`
}

type Inspector interface {
	Inspect(context.Context, ModuleWork) ModuleResult
}

type Committer interface {
	Commit(context.Context, []ModuleResult) error
}

type RetryEntry struct {
	Error    string `json:"error"`
	Attempts int    `json:"attempts"`
}

type ImportSnapshot struct {
	Cursor               uint64
	CatalogOffset        int64
	CatalogSize          uint64
	CatalogComplete      bool
	CatalogSince         string
	RefreshCursor        uint64
	RefreshCatalogOffset int64
	ModulesFile          string
	Retries              map[string]RetryEntry
	Unavailable          map[string]string
	Observations         []Observation
	// Checks maps a module to its encoded Check (see EncodeCheck).
	Checks map[string]string
	// FeedCursor is the opaque position of the registry change feed.
	FeedCursor string
	// FeedPending holds announced changes not yet committed as observations.
	FeedPending map[string]FeedEntry
	// DueFloor makes every check older than this day due (feed resynchronisation).
	DueFloor int
}

type Snapshot struct {
	ImportSnapshot
	Generation      uint64
	Processed       uint64
	DownloadedBytes uint64
}
