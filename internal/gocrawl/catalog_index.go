package gocrawl

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	bolt "go.etcd.io/bbolt"
)

const (
	catalogIndexVersion  = uint64(2)
	catalogIndexHeader   = 64
	catalogIndexRecord   = 24
	catalogTailBatchSize = 10_000
)

var catalogIndexMagic = [8]byte{'G', 'O', 'C', 'A', 'T', 'I', 'D', 'X'}

type catalogRecord struct {
	hash   [16]byte
	offset uint64
}

type CatalogIndex struct {
	index    *os.File
	catalog  *os.File
	baseSize int64
	count    uint64
	digest   [32]byte
}

func (s *BoltStore) SyncCatalog(ctx context.Context, path string) error {
	index, err := openOrBuildCatalogIndex(ctx, path)
	if err != nil {
		return err
	}
	if s.catalog != nil {
		_ = s.catalog.Close()
	}
	s.catalog = index
	stat, err := index.catalog.Stat()
	if err != nil {
		return err
	}
	var schema uint64
	var baseSize int64
	var offset int64
	var deltaCount uint64
	var baseDigest []byte
	if err := s.db.View(func(tx *bolt.Tx) error {
		meta := tx.Bucket(metaBucket)
		schema = getUint(meta, "catalog_index_version")
		baseSize = getInt64(meta, "catalog_base_size")
		offset = getInt64(meta, "catalog_delta_offset")
		deltaCount = getUint(meta, "catalog_delta_count")
		baseDigest = append([]byte(nil), meta.Get([]byte("catalog_base_digest"))...)
		return nil
	}); err != nil {
		return err
	}
	if schema != catalogIndexVersion || baseSize != index.baseSize || !bytes.Equal(baseDigest, index.digest[:]) {
		if err := s.db.Update(func(tx *bolt.Tx) error {
			if err := tx.DeleteBucket(catalogBucket); err != nil && !errors.Is(err, bolt.ErrBucketNotFound) {
				return err
			}
			if _, err := tx.CreateBucket(catalogBucket); err != nil {
				return err
			}
			meta := tx.Bucket(metaBucket)
			for key, value := range map[string]uint64{
				"catalog_index_version": catalogIndexVersion,
				"catalog_base_size":     uint64(index.baseSize),
				"catalog_base_count":    index.count,
				"catalog_delta_offset":  uint64(index.baseSize),
				"catalog_delta_count":   0,
				"catalog_size":          index.count,
			} {
				if err := putUint(meta, key, value); err != nil {
					return err
				}
			}
			return meta.Put([]byte("catalog_base_digest"), index.digest[:])
		}); err != nil {
			return err
		}
		offset = index.baseSize
		deltaCount = 0
	}
	if offset < index.baseSize || offset > stat.Size() {
		return fmt.Errorf("catalog tail is inconsistent: base=%d indexed=%d size=%d", index.baseSize, offset, stat.Size())
	}
	return s.syncCatalogTail(ctx, offset, deltaCount, stat.Size())
}

func (s *BoltStore) syncCatalogTail(ctx context.Context, offset int64, deltaCount uint64, fileSize int64) error {
	if _, err := s.catalog.catalog.Seek(offset, io.SeekStart); err != nil {
		return err
	}
	reader := bufio.NewReaderSize(s.catalog.catalog, 256*1024)
	for offset < fileSize {
		paths := make([]string, 0, catalogTailBatchSize)
		batchEnd := offset
		for len(paths) < cap(paths) && batchEnd < fileSize {
			line, err := reader.ReadString('\n')
			batchEnd += int64(len(line))
			module := strings.TrimSpace(line)
			if module != "" {
				paths = append(paths, module)
			}
			if err != nil {
				if !errors.Is(err, io.EOF) {
					return err
				}
				break
			}
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		for _, module := range paths {
			contains, err := s.catalog.Contains(module)
			if err != nil {
				return err
			}
			if contains {
				return fmt.Errorf("catalog tail duplicates base module %q", module)
			}
		}
		if err := s.db.Update(func(tx *bolt.Tx) error {
			bucket := tx.Bucket(catalogBucket)
			for _, module := range paths {
				if bucket.Get([]byte(module)) != nil {
					return fmt.Errorf("duplicate catalog tail module %q", module)
				}
				if err := bucket.Put([]byte(module), []byte{1}); err != nil {
					return err
				}
			}
			deltaCount += uint64(len(paths))
			meta := tx.Bucket(metaBucket)
			if err := putInt64(meta, "catalog_delta_offset", batchEnd); err != nil {
				return err
			}
			if err := putUint(meta, "catalog_delta_count", deltaCount); err != nil {
				return err
			}
			return putUint(meta, "catalog_size", s.catalog.count+deltaCount)
		}); err != nil {
			return err
		}
		offset = batchEnd
	}
	return nil
}

func (s *BoltStore) FilterNewCatalogModules(ctx context.Context, modules []string) ([]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if s.catalog == nil {
		return nil, errors.New("catalog is not synchronized")
	}
	unique := make(map[string]struct{}, len(modules))
	candidates := make([]string, 0, len(modules))
	for _, module := range modules {
		module = strings.TrimSpace(module)
		if module == "" {
			continue
		}
		if _, duplicate := unique[module]; duplicate {
			continue
		}
		unique[module] = struct{}{}
		contains, err := s.catalog.Contains(module)
		if err != nil {
			return nil, err
		}
		if !contains {
			candidates = append(candidates, module)
		}
	}
	result := make([]string, 0, len(candidates))
	err := s.db.View(func(tx *bolt.Tx) error {
		bucket := tx.Bucket(catalogBucket)
		for _, module := range candidates {
			if bucket.Get([]byte(module)) == nil {
				result = append(result, module)
			}
		}
		return nil
	})
	return result, err
}

func (s *BoltStore) CommitCatalogPage(
	ctx context.Context,
	newModules []string,
	fileEnd int64,
	since string,
	complete bool,
) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		meta := tx.Bucket(metaBucket)
		indexedEnd := getInt64(meta, "catalog_delta_offset")
		if fileEnd < indexedEnd {
			return fmt.Errorf("catalog end regressed: got %d, have %d", fileEnd, indexedEnd)
		}
		bucket := tx.Bucket(catalogBucket)
		for _, module := range newModules {
			if bucket.Get([]byte(module)) != nil {
				return fmt.Errorf("duplicate catalog module %q", module)
			}
			if err := bucket.Put([]byte(module), []byte{1}); err != nil {
				return err
			}
		}
		deltaCount := getUint(meta, "catalog_delta_count") + uint64(len(newModules))
		if err := putInt64(meta, "catalog_delta_offset", fileEnd); err != nil {
			return err
		}
		if err := putUint(meta, "catalog_delta_count", deltaCount); err != nil {
			return err
		}
		if err := putUint(meta, "catalog_size", getUint(meta, "catalog_base_count")+deltaCount); err != nil {
			return err
		}
		if since != "" {
			if err := meta.Put([]byte("catalog_since"), []byte(since)); err != nil {
				return err
			}
		}
		return putBool(meta, "catalog_complete", complete)
	})
}

func openOrBuildCatalogIndex(ctx context.Context, catalogPath string) (*CatalogIndex, error) {
	indexPath := catalogPath + ".index"
	index, err := openCatalogIndex(ctx, catalogPath, indexPath)
	if err == nil {
		return index, nil
	}
	if buildErr := buildCatalogIndex(ctx, catalogPath, indexPath); buildErr != nil {
		return nil, fmt.Errorf("open catalog index (%v), rebuild: %w", err, buildErr)
	}
	return openCatalogIndex(ctx, catalogPath, indexPath)
}

func buildCatalogIndex(ctx context.Context, catalogPath, indexPath string) error {
	catalog, err := os.Open(catalogPath)
	if err != nil {
		return err
	}
	defer catalog.Close()
	stat, err := catalog.Stat()
	if err != nil {
		return err
	}
	baseSize := stat.Size()
	reader := bufio.NewReaderSize(io.LimitReader(catalog, baseSize), 256*1024)
	digest := sha256.New()
	records := make([]catalogRecord, 0, max(1, int(baseSize/32)))
	var offset int64
	for offset < baseSize {
		line, readErr := reader.ReadString('\n')
		_, _ = digest.Write([]byte(line))
		module := strings.TrimSpace(line)
		if module != "" {
			hash := sha256.Sum256([]byte(module))
			var prefix [16]byte
			copy(prefix[:], hash[:16])
			records = append(records, catalogRecord{hash: prefix, offset: uint64(offset)})
		}
		offset += int64(len(line))
		if readErr != nil && !errors.Is(readErr, io.EOF) {
			return readErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
	sort.Slice(records, func(i, j int) bool {
		if comparison := bytes.Compare(records[i].hash[:], records[j].hash[:]); comparison != 0 {
			return comparison < 0
		}
		return records[i].offset < records[j].offset
	})
	if err := os.MkdirAll(filepath.Dir(indexPath), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(indexPath), "."+filepath.Base(indexPath)+".*.tmp")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o644); err != nil {
		_ = temporary.Close()
		return err
	}
	header := make([]byte, catalogIndexHeader)
	copy(header[:8], catalogIndexMagic[:])
	binary.BigEndian.PutUint64(header[8:16], catalogIndexVersion)
	binary.BigEndian.PutUint64(header[16:24], uint64(baseSize))
	binary.BigEndian.PutUint64(header[24:32], uint64(len(records)))
	copy(header[32:64], digest.Sum(nil))
	buffered := bufio.NewWriterSize(temporary, 256*1024)
	if _, err := buffered.Write(header); err != nil {
		_ = temporary.Close()
		return err
	}
	encoded := make([]byte, catalogIndexRecord)
	for _, record := range records {
		copy(encoded[:16], record.hash[:])
		binary.BigEndian.PutUint64(encoded[16:24], record.offset)
		if _, err := buffered.Write(encoded); err != nil {
			_ = temporary.Close()
			return err
		}
	}
	if err := buffered.Flush(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, indexPath); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(indexPath))
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func openCatalogIndex(ctx context.Context, catalogPath, indexPath string) (*CatalogIndex, error) {
	indexFile, err := os.Open(indexPath)
	if err != nil {
		return nil, err
	}
	fail := func(err error) (*CatalogIndex, error) {
		_ = indexFile.Close()
		return nil, err
	}
	header := make([]byte, catalogIndexHeader)
	if _, err := io.ReadFull(indexFile, header); err != nil {
		return fail(err)
	}
	if !bytes.Equal(header[:8], catalogIndexMagic[:]) || binary.BigEndian.Uint64(header[8:16]) != catalogIndexVersion {
		return fail(errors.New("unsupported catalog index header"))
	}
	baseSize := int64(binary.BigEndian.Uint64(header[16:24]))
	count := binary.BigEndian.Uint64(header[24:32])
	indexStat, err := indexFile.Stat()
	if err != nil {
		return fail(err)
	}
	if indexStat.Size() != catalogIndexHeader+int64(count)*catalogIndexRecord {
		return fail(fmt.Errorf("catalog index size mismatch"))
	}
	catalog, err := os.Open(catalogPath)
	if err != nil {
		return fail(err)
	}
	catalogStat, err := catalog.Stat()
	if err != nil {
		_ = catalog.Close()
		return fail(err)
	}
	if baseSize < 0 || baseSize > catalogStat.Size() {
		_ = catalog.Close()
		return fail(fmt.Errorf("catalog index base %d exceeds file size %d", baseSize, catalogStat.Size()))
	}
	hash := sha256.New()
	if _, err := io.CopyN(hash, catalog, baseSize); err != nil {
		_ = catalog.Close()
		return fail(err)
	}
	if err := ctx.Err(); err != nil {
		_ = catalog.Close()
		return fail(err)
	}
	if !bytes.Equal(hash.Sum(nil), header[32:64]) {
		_ = catalog.Close()
		return fail(errors.New("catalog prefix digest mismatch"))
	}
	var digest [32]byte
	copy(digest[:], header[32:64])
	return &CatalogIndex{
		index: indexFile, catalog: catalog, baseSize: baseSize, count: count, digest: digest,
	}, nil
}

func (i *CatalogIndex) Close() error {
	indexErr := i.index.Close()
	catalogErr := i.catalog.Close()
	if indexErr != nil {
		return indexErr
	}
	return catalogErr
}

func (i *CatalogIndex) Contains(module string) (bool, error) {
	hash := sha256.Sum256([]byte(module))
	prefix := hash[:16]
	lower, err := i.lowerBound(prefix)
	if err != nil {
		return false, err
	}
	for position := lower; position < i.count; position++ {
		record, err := i.readRecord(position)
		if err != nil {
			return false, err
		}
		comparison := bytes.Compare(record.hash[:], prefix)
		if comparison != 0 {
			return false, nil
		}
		candidate, err := i.moduleAt(int64(record.offset))
		if err != nil {
			return false, err
		}
		if candidate == module {
			return true, nil
		}
	}
	return false, nil
}

func (i *CatalogIndex) lowerBound(hash []byte) (uint64, error) {
	left, right := uint64(0), i.count
	for left < right {
		middle := left + (right-left)/2
		record, err := i.readRecord(middle)
		if err != nil {
			return 0, err
		}
		if bytes.Compare(record.hash[:], hash) < 0 {
			left = middle + 1
		} else {
			right = middle
		}
	}
	return left, nil
}

func (i *CatalogIndex) readRecord(position uint64) (catalogRecord, error) {
	encoded := make([]byte, catalogIndexRecord)
	offset := int64(catalogIndexHeader) + int64(position)*catalogIndexRecord
	if _, err := i.index.ReadAt(encoded, offset); err != nil {
		return catalogRecord{}, err
	}
	var record catalogRecord
	copy(record.hash[:], encoded[:16])
	record.offset = binary.BigEndian.Uint64(encoded[16:24])
	return record, nil
}

func (i *CatalogIndex) moduleAt(offset int64) (string, error) {
	if offset < 0 || offset >= i.baseSize {
		return "", fmt.Errorf("catalog record offset %d outside base", offset)
	}
	reader := bufio.NewReader(io.NewSectionReader(i.catalog, offset, i.baseSize-offset))
	line, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", err
	}
	return strings.TrimSpace(line), nil
}
