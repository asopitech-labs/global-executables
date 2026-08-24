package gocrawl

import (
	"context"
	"sync"
	"testing"
	"time"
)

type inspectorFunc func(context.Context, ModuleWork) ModuleResult

func (f inspectorFunc) Inspect(ctx context.Context, work ModuleWork) ModuleResult {
	return f(ctx, work)
}

type recordingCommitter struct {
	mu      sync.Mutex
	results []ModuleResult
}

func (c *recordingCommitter) Commit(_ context.Context, results []ModuleResult) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.results = append(c.results, results...)
	return nil
}

func TestCoordinatorKeepsWorkersBusyAndCommitsInOrder(t *testing.T) {
	releaseFirst := make(chan struct{})
	thirdStarted := make(chan struct{})
	var thirdOnce sync.Once
	inspector := inspectorFunc(func(_ context.Context, work ModuleWork) ModuleResult {
		if work.Order == 0 {
			<-releaseFirst
		}
		if work.Order == 2 {
			thirdOnce.Do(func() { close(thirdStarted) })
		}
		return ModuleResult{Work: work, Verdict: VerdictSuccess}
	})
	committer := &recordingCommitter{}
	coordinator := Coordinator{Workers: 2, MaxInFlight: 4, CommitBatch: 1}
	works := []ModuleWork{
		{Order: 0, CatalogIndex: 10, Module: "example.com/0"},
		{Order: 1, CatalogIndex: 11, Module: "example.com/1"},
		{Order: 2, CatalogIndex: 12, Module: "example.com/2"},
	}

	done := make(chan error, 1)
	go func() { done <- coordinator.Run(context.Background(), works, inspector, committer) }()
	select {
	case <-thirdStarted:
	case <-time.After(time.Second):
		close(releaseFirst)
		t.Fatal("an idle worker did not start queued work behind the slow first result")
	}
	close(releaseFirst)
	if err := <-done; err != nil {
		t.Fatalf("run: %v", err)
	}

	committer.mu.Lock()
	defer committer.mu.Unlock()
	if len(committer.results) != 3 {
		t.Fatalf("committed %d results, want 3", len(committer.results))
	}
	for i, result := range committer.results {
		if result.Work.Order != uint64(i) {
			t.Fatalf("commit[%d] order=%d", i, result.Work.Order)
		}
	}
}

func TestCoordinatorDoesNotCommitPastCanceledPrefix(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	secondStarted := make(chan struct{})
	inspector := inspectorFunc(func(ctx context.Context, work ModuleWork) ModuleResult {
		if work.Order == 0 {
			<-ctx.Done()
			return ModuleResult{Work: work, Verdict: VerdictCanceled}
		}
		close(secondStarted)
		return ModuleResult{Work: work, Verdict: VerdictSuccess}
	})
	committer := &recordingCommitter{}
	done := make(chan error, 1)
	go func() {
		done <- (Coordinator{Workers: 2, MaxInFlight: 2, CommitBatch: 1}).Run(
			ctx,
			[]ModuleWork{{Order: 0, CatalogIndex: 0}, {Order: 1, CatalogIndex: 1}},
			inspector,
			committer,
		)
	}()
	<-secondStarted
	cancel()
	if err := <-done; err == nil {
		t.Fatal("canceled run returned nil")
	}
	if len(committer.results) != 0 {
		t.Fatalf("committed past a canceled prefix: %#v", committer.results)
	}
}
