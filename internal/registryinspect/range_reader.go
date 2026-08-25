package registryinspect

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"
)

type httpRangeReaderAt struct {
	ctx       context.Context
	http      *requester
	url       string
	size      int64
	blockSize int64
	maxBlocks int

	mu         sync.Mutex
	blocks     map[int64][]byte
	order      []int64
	downloaded int64
}

func newHTTPRangeReaderAt(ctx context.Context, requester *requester, target string, size int64) *httpRangeReaderAt {
	return &httpRangeReaderAt{ctx: ctx, http: requester, url: target, size: size,
		blockSize: requester.config.RangeBlockSize, maxBlocks: requester.config.RangeCacheBlocks,
		blocks: make(map[int64][]byte)}
}

func (r *httpRangeReaderAt) ReadAt(buffer []byte, offset int64) (int, error) {
	if offset >= r.size {
		return 0, io.EOF
	}
	written := 0
	for written < len(buffer) && offset+int64(written) < r.size {
		position := offset + int64(written)
		blockStart := position / r.blockSize * r.blockSize
		block, err := r.block(blockStart)
		if err != nil {
			return written, err
		}
		inside := int(position - blockStart)
		copied := copy(buffer[written:], block[inside:])
		written += copied
	}
	if written < len(buffer) {
		return written, io.EOF
	}
	return written, nil
}

func (r *httpRangeReaderAt) block(start int64) ([]byte, error) {
	r.mu.Lock()
	if block, exists := r.blocks[start]; exists {
		r.mu.Unlock()
		return block, nil
	}
	r.mu.Unlock()
	end := min(start+r.blockSize-1, r.size-1)
	headers := make(http.Header)
	headers.Set("Range", fmt.Sprintf("bytes=%d-%d", start, end))
	response, err := r.http.request(r.ctx, http.MethodGet, r.url, headers, end-start+1)
	if err != nil {
		return nil, err
	}
	if response.statusCode != http.StatusPartialContent {
		return nil, fmt.Errorf("host ignored range request for %s: HTTP %d", r.url, response.statusCode)
	}
	if got := response.header.Get("Content-Range"); got == "" {
		return nil, fmt.Errorf("range response omitted Content-Range for %s", r.url)
	}
	expected := int(end - start + 1)
	if len(response.body) != expected {
		return nil, fmt.Errorf("range response length %d, want %s", len(response.body), strconv.Itoa(expected))
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, exists := r.blocks[start]; exists {
		return existing, nil
	}
	r.blocks[start] = response.body
	r.order = append(r.order, start)
	r.downloaded += int64(len(response.body))
	if len(r.order) > r.maxBlocks {
		delete(r.blocks, r.order[0])
		r.order = r.order[1:]
	}
	return response.body, nil
}

func (r *httpRangeReaderAt) Downloaded() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.downloaded
}
