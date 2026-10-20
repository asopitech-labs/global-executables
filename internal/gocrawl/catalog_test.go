package gocrawl

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCatalogBatchResumesFromCommittedByteOffset(t *testing.T) {
	path := filepath.Join(t.TempDir(), "go-modules.txt")
	if err := os.WriteFile(path, []byte("example.com/a\nexample.com/b\nexample.com/c\nexample.com/d\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	offset, err := LocateCatalogOffset(path, 2)
	if err != nil {
		t.Fatal(err)
	}
	works, err := ReadCatalogBatch(path, 2, offset, 2, 7)
	if err != nil {
		t.Fatal(err)
	}
	if len(works) != 2 || works[0].Module != "example.com/c" || works[1].Module != "example.com/d" {
		t.Fatalf("works=%+v", works)
	}
	if works[0].Order != 7 || works[1].CatalogIndex != 3 || works[1].CatalogOffset <= works[0].CatalogOffset {
		t.Fatalf("works=%+v", works)
	}
}
