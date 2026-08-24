package gocrawl

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

func LocateCatalogOffset(path string, cursor uint64) (int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	reader := bufio.NewReader(file)
	var offset int64
	var index uint64
	for index < cursor {
		line, readErr := reader.ReadString('\n')
		offset += int64(len(line))
		if strings.TrimSpace(line) != "" {
			index++
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				return 0, fmt.Errorf("catalog has %d modules, cannot locate cursor %d", index, cursor)
			}
			return 0, readErr
		}
	}
	return offset, nil
}

func ReadCatalogBatch(path string, cursor uint64, offset int64, limit int, startOrder uint64) ([]ModuleWork, error) {
	if limit <= 0 {
		return nil, nil
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return nil, err
	}
	reader := bufio.NewReader(file)
	works := make([]ModuleWork, 0, limit)
	currentOffset := offset
	for len(works) < limit {
		line, readErr := reader.ReadString('\n')
		currentOffset += int64(len(line))
		module := strings.TrimSpace(line)
		if module != "" {
			works = append(works, ModuleWork{
				Order:         startOrder + uint64(len(works)),
				CatalogIndex:  cursor + uint64(len(works)),
				CatalogOffset: currentOffset,
				Module:        module,
				Attempt:       1,
			})
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			return nil, readErr
		}
	}
	return works, nil
}

func CountCatalog(path string) (uint64, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	buffer := make([]byte, 64*1024)
	scanner.Buffer(buffer, 4*1024*1024)
	var count uint64
	for scanner.Scan() {
		if strings.TrimSpace(scanner.Text()) != "" {
			count++
		}
	}
	return count, scanner.Err()
}
