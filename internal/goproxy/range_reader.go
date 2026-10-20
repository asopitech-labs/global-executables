package goproxy

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"sync"
	"sync/atomic"
)

type httpRangeReaderAt struct {
	ctx        context.Context
	inspector  *Inspector
	url        string
	size       int64
	blockSize  int64
	maxBlocks  int
	mu         sync.Mutex
	blocks     map[int64]rangeBlock
	clock      uint64
	full       []byte
	downloaded atomic.Int64
}

type rangeBlock struct {
	body     []byte
	lastUsed uint64
}

func newHTTPRangeReaderAt(ctx context.Context, inspector *Inspector, url string, size, blockSize int64, maxBlocks int) *httpRangeReaderAt {
	return &httpRangeReaderAt{
		ctx: ctx, inspector: inspector, url: url, size: size, blockSize: blockSize, maxBlocks: maxBlocks,
		blocks: make(map[int64]rangeBlock),
	}
}

func (r *httpRangeReaderAt) ReadAt(p []byte, offset int64) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if offset < 0 {
		return 0, fmt.Errorf("negative range offset %d", offset)
	}
	if offset >= r.size {
		return 0, io.EOF
	}
	if r.full != nil {
		n := copy(p, r.full[offset:])
		if n < len(p) {
			return n, io.EOF
		}
		return n, nil
	}
	written := 0
	for written < len(p) && offset+int64(written) < r.size {
		position := offset + int64(written)
		blockStart := (position / r.blockSize) * r.blockSize
		cached, exists := r.blocks[blockStart]
		r.clock++
		var block []byte
		if exists {
			cached.lastUsed = r.clock
			r.blocks[blockStart] = cached
			block = cached.body
		}
		if !exists {
			blockEnd := min(blockStart+r.blockSize-1, r.size-1)
			headers := make(http.Header)
			headers.Set("Range", fmt.Sprintf("bytes=%d-%d", blockStart, blockEnd))
			responseLimit := min(r.size, r.inspector.config.MaxFullDownloadBytes)
			response, err := r.inspector.request(r.ctx, http.MethodGet, r.url, headers, responseLimit)
			if err != nil {
				return written, err
			}
			r.downloaded.Add(int64(len(response.Body)))
			switch response.StatusCode {
			case http.StatusPartialContent:
				block = response.Body
				r.putBlock(blockStart, block)
			case http.StatusOK:
				if int64(len(response.Body)) != r.size {
					return written, fmt.Errorf("range fallback returned %d bytes, want %d", len(response.Body), r.size)
				}
				r.full = response.Body
				continue
			default:
				return written, fmt.Errorf("range request returned HTTP %d", response.StatusCode)
			}
		}
		inside := position - blockStart
		if inside >= int64(len(block)) {
			return written, io.ErrUnexpectedEOF
		}
		available := len(block) - int(inside)
		remaining := len(p) - written
		copied := copy(p[written:written+min(available, remaining)], block[inside:])
		written += copied
	}
	if written < len(p) {
		return written, io.EOF
	}
	return written, nil
}

func (r *httpRangeReaderAt) putBlock(offset int64, body []byte) {
	if r.maxBlocks <= 0 {
		return
	}
	if len(r.blocks) >= r.maxBlocks {
		var oldestOffset int64
		oldestUse := ^uint64(0)
		for candidateOffset, candidate := range r.blocks {
			if candidate.lastUsed < oldestUse {
				oldestOffset = candidateOffset
				oldestUse = candidate.lastUsed
			}
		}
		delete(r.blocks, oldestOffset)
	}
	r.blocks[offset] = rangeBlock{body: body, lastUsed: r.clock}
}

func (r *httpRangeReaderAt) Downloaded() int64 {
	return r.downloaded.Load()
}
