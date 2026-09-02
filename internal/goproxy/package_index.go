package goproxy

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"path"
	"slices"
	"strings"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func (i *Inspector) inspectPackageIndex(
	ctx context.Context,
	modulePath, version string,
) ([]gocrawl.Observation, int64, bool, error) {
	commands := make(map[string]struct{})
	packageCount := 0
	var downloaded int64
	var token string
	for {
		if err := i.waitForPackageIndex(ctx); err != nil {
			return nil, downloaded, false, err
		}
		endpoint, err := url.Parse(i.config.PackageIndexURL + "/v1/packages/" + modulePath)
		if err != nil {
			return nil, downloaded, false, err
		}
		query := endpoint.Query()
		query.Set("version", version)
		query.Set("limit", "1000")
		query.Set("filter", `name == "main"`)
		if token != "" {
			query.Set("token", token)
		}
		endpoint.RawQuery = query.Encode()
		response, err := i.request(ctx, http.MethodGet, endpoint.String(), nil, 8*1024*1024)
		downloaded += int64(len(response.Body))
		if err != nil {
			return nil, downloaded, false, err
		}
		var payload struct {
			ModulePath string `json:"modulePath"`
			Version    string `json:"version"`
			Packages   struct {
				Items []struct {
					Path string `json:"path"`
					Name string `json:"name"`
				} `json:"items"`
				Total         int    `json:"total"`
				NextPageToken string `json:"nextPageToken"`
			} `json:"packages"`
		}
		if err := json.Unmarshal(response.Body, &payload); err != nil {
			return nil, downloaded, false, err
		}
		if payload.ModulePath != modulePath || payload.Version != version {
			return nil, downloaded, false, fmt.Errorf("package index returned %s@%s for %s@%s", payload.ModulePath, payload.Version, modulePath, version)
		}
		for _, item := range payload.Packages.Items {
			if item.Name != "main" || item.Path != modulePath && !strings.HasPrefix(item.Path, modulePath+"/") {
				return nil, downloaded, false, fmt.Errorf("package index returned invalid package %s (%s)", item.Path, item.Name)
			}
			packageCount++
			commands[path.Base(item.Path)] = struct{}{}
		}
		token = payload.Packages.NextPageToken
		if token == "" {
			if payload.Packages.Total >= 0 && packageCount < payload.Packages.Total {
				return nil, downloaded, false, fmt.Errorf("package index returned %d of %d packages", packageCount, payload.Packages.Total)
			}
			commandNames := make([]string, 0, len(commands))
			for command := range commands {
				commandNames = append(commandNames, command)
			}
			slices.Sort(commandNames)
			return observationsForCommands(commandNames, modulePath, version, endpoint.String()), downloaded, true, nil
		}
	}
}

func (i *Inspector) waitForPackageIndex(ctx context.Context) error {
	i.packageIndexMu.Lock()
	now := time.Now()
	wait := max(i.nextPackageRequest.Sub(now), 0)
	i.nextPackageRequest = now.Add(wait + i.config.PackageIndexInterval)
	i.packageIndexMu.Unlock()
	return i.sleep(ctx, wait)
}
