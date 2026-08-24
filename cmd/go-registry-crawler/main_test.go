package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestRunReportsBootstrapVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer

	if code := run([]string{"--version"}, &stdout, &stderr); code != 0 {
		t.Fatalf("run code = %d, want 0; stderr = %q", code, stderr.String())
	}
	if got := strings.TrimSpace(stdout.String()); got != "go-registry-crawler dev" {
		t.Fatalf("version = %q", got)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestRunCannotStartCrawlerBeforeImplementation(t *testing.T) {
	var stdout, stderr bytes.Buffer

	if code := run(nil, &stdout, &stderr); code != 2 {
		t.Fatalf("run code = %d, want 2; stderr = %q", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "not implemented") {
		t.Fatalf("stderr = %q", stderr.String())
	}
	if stdout.Len() != 0 {
		t.Fatalf("stdout = %q", stdout.String())
	}
}
