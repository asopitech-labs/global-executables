package main

import (
	"fmt"
	"io"
	"os"
)

var version = "dev"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 1 && args[0] == "--version" {
		_, _ = fmt.Fprintf(stdout, "go-registry-crawler %s\n", version)
		return 0
	}

	_, _ = fmt.Fprintln(stderr, "go-registry-crawler: crawler not implemented; production routing is disabled")
	return 2
}
