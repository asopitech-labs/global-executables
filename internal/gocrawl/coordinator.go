package gocrawl

import (
	"context"
	"errors"
	"fmt"
	"sync"
)

type Coordinator struct {
	Workers     int
	MaxInFlight int
	CommitBatch int
}

func (c Coordinator) Run(ctx context.Context, works []ModuleWork, inspector Inspector, committer Committer) error {
	if len(works) == 0 {
		return nil
	}
	if inspector == nil || committer == nil {
		return errors.New("inspector and committer are required")
	}
	if c.Workers <= 0 {
		c.Workers = 1
	}
	if c.MaxInFlight < c.Workers {
		c.MaxInFlight = c.Workers
	}
	if c.CommitBatch <= 0 {
		c.CommitBatch = 1
	}

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	workCh := make(chan ModuleWork)
	resultCh := make(chan ModuleResult, c.MaxInFlight)
	var workers sync.WaitGroup
	for range c.Workers {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for work := range workCh {
				result := inspector.Inspect(runCtx, work)
				select {
				case resultCh <- result:
				case <-runCtx.Done():
					return
				}
			}
		}()
	}
	defer func() {
		close(workCh)
		cancel()
		workers.Wait()
	}()

	submitted := 0
	active := 0
	committed := 0
	pending := make(map[uint64]ModuleResult, c.MaxInFlight)
	ready := make([]ModuleResult, 0, c.CommitBatch)

	commitReady := func(force bool) error {
		for len(ready) >= c.CommitBatch || (force && len(ready) > 0) {
			size := min(c.CommitBatch, len(ready))
			if err := committer.Commit(runCtx, ready[:size]); err != nil {
				return err
			}
			ready = ready[size:]
			committed += size
		}
		return nil
	}

	for committed < len(works) {
		var sendCh chan ModuleWork
		var next ModuleWork
		if submitted < len(works) && active < c.MaxInFlight {
			sendCh = workCh
			next = works[submitted]
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case sendCh <- next:
			submitted++
			active++
		case result := <-resultCh:
			active--
			if _, exists := pending[result.Work.Order]; exists {
				return fmt.Errorf("duplicate result order %d", result.Work.Order)
			}
			pending[result.Work.Order] = result

			for committed+len(ready) < len(works) {
				expected := works[committed+len(ready)]
				result, exists := pending[expected.Order]
				if !exists {
					break
				}
				if result.Work.Order != expected.Order || result.Work.Module != expected.Module {
					return fmt.Errorf("result does not match submitted work order %d", expected.Order)
				}
				delete(pending, expected.Order)
				if result.Verdict == VerdictCanceled {
					if err := commitReady(true); err != nil {
						return err
					}
					if err := ctx.Err(); err != nil {
						return err
					}
					return context.Canceled
				}
				ready = append(ready, result)
			}
			if err := commitReady(false); err != nil {
				return err
			}
			if submitted == len(works) && active == 0 {
				if err := commitReady(true); err != nil {
					return err
				}
			}
		}
	}
	return nil
}
