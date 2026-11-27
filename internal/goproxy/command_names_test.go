package goproxy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/mod/module"
)

// The dictionary pipeline (Python) drops Go names that `go install` can never
// produce. Its rule must match the check commandCandidates applies, so both read
// the same expectations.
func TestCommandNameFixtureMatchesImportPathCheck(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "command-names.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Installable   []string `json:"installable"`
		Uninstallable []string `json:"uninstallable"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Installable) == 0 || len(fixture.Uninstallable) == 0 {
		t.Fatal("fixture lists no names")
	}
	for _, name := range fixture.Installable {
		if err := module.CheckImportPath("example.com/m/" + name); err != nil {
			t.Errorf("%q expected installable: %v", name, err)
		}
	}
	for _, name := range fixture.Uninstallable {
		if module.CheckImportPath("example.com/m/"+name) == nil {
			t.Errorf("%q expected uninstallable", name)
		}
	}
}
