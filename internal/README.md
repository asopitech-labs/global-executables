# Internal Go packages

Crawler contracts, transactional storage, catalog recovery, and orchestration belong
in `internal/gocrawl`; Go proxy, module-index, Range ZIP, and archive inspection
adapters belong in `internal/goproxy`. The command composition root is
`cmd/go-registry-crawler`.
