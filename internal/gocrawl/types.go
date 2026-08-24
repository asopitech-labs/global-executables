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
	Attempt       int
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
	Cursor          uint64
	CatalogOffset   int64
	CatalogSize     uint64
	CatalogComplete bool
	CatalogSince    string
	ModulesFile     string
	Retries         map[string]RetryEntry
	Unavailable     map[string]string
	Observations    []Observation
}

type Snapshot struct {
	ImportSnapshot
	Generation      uint64
	Processed       uint64
	DownloadedBytes uint64
}
