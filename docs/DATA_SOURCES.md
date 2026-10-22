# Data sources and rights

This page lists every external source that the Global Executables collectors,
crawlers, workflows and tools read, what is taken from each, the license and
terms that apply, the attribution each source asks for, and whether this
project complies today. It was compiled from the code on `main` and from the
primary documents linked in each section, checked on 2026-10-19. It is a
record of what we found, not legal advice. Anything we could not confirm from
a primary document is marked **unverified**.

`tests/test_data_sources_doc.py` fails when a host that appears in a collector,
crawler, tool, workflow, Dockerfile or the playground is missing from this page.

## Installation-method references

The installation resolver links package-manager command documentation for users.
It does not fetch these pages or copy their content into the dictionary. These
links establish command syntax, not whether an indexed package is available or
installs the expected executable on a particular target:

- `manpages.debian.org`: Debian apt-get manual.
- `docs.brew.sh`: Homebrew install command manual.
- `docs.npmjs.com`: npm install command manual.
- `pip.pypa.io`: pip install command manual.

## What the dataset contains

The published dataset (the `dictionary` branch, the GitHub Pages playground and
the MCP server) contains, for each executable name: the name, the ecosystem,
the provider package name and version, a repository or homepage URL as the
upstream declares it, the source URL the evidence came from, and how the name
was derived. It does not contain package archives, source code, descriptions,
READMEs, maintainer data or download counts. A command name and the fact that
a package installs it are facts about the package.

### Inventory observations are separate from upstream acquisition

Inspecting packages already present on a local PC or inside a container, and
publishing their names, versions, executable names and observed state, is the
inventory workflow. This page does not classify that observation or factual
state publication as a rights gap. It does not redistribute package contents.

The rights and policy questions recorded below concern different actions:
requesting data from an external service, copying a third-party dataset, or
redistributing third-party source/fixture excerpts. Image/OS provisioning and
runtime license conditions belong to provisioning, not to the inventory output.
An upstream software license or an unreviewed host license must not be used on
its own to label local package inspection or publication of factual state as
restricted. A missing external-service terms reference is likewise not a
finding against the local inventory workflow.

## Summary

Status values: **OK** means we found no conflict with the source's published
terms. **Gap** means something has to change or be decided; the per-source
notes say what. **Unverified** means the source publishes no terms we could find
or a point could not be confirmed.

| Source | Hosts | What we take | Data license | Terms and usage policy | Attribution required | Status |
|---|---|---|---|---|---|---|
| ecosyste.ms Packages | packages.ecosyste.ms | Names of the 2,295 "critical" npm packages, scraped from HTML pages | CC BY-SA 4.0 | ecosyste.ms Terms of Service; polite pool needs a `mailto` | Yes: "ecosyste.ms" as the source, CC BY-SA 4.0 | **Gap** (share-alike, scraping clause) |
| npm registry | registry.npmjs.org, replicate.npmjs.com | `bin` of each listed package's latest version; change feed and package-name index | Not stated for metadata; package contents under each package's license | npm Open-Source Terms; Public APIs allowed; under 5 million requests a month | No | OK |
| PyPI | pypi.org, files.pythonhosted.org | Project list, JSON metadata, entry points read from wheels and sdists | Not stated for metadata; files under each project's license | PyPI Terms of Service, API policies | No | OK |
| crates.io | static.crates.io, index.crates.io, crates.io | `bin_names` from the daily database dump | Not stated in the dump | crates.io Data Access Policy | No | OK |
| Go module mirror and index | proxy.golang.org, index.golang.org | Module list, versions, `.mod` files, module zips (`package main` directories) | Each module's own license | Go module services page; `/cached-only` for bulk use | No | OK |
| pkg.go.dev API | pkg.go.dev | `name == "main"` packages of a module | Not stated | API documentation: 45 queries per second per IP block | No | OK; terms of service **unverified** |
| RubyGems.org | rubygems.org | Gem metadata JSON and `.gem` files (`executables`) | Not stated for metadata; gems under each gem's license | RubyGems.org Terms of Service; 10 requests per second for API | No | OK |
| Packagist | repo.packagist.org, packagist.org | Composer v2 metadata (`bin`), metadata change feed | Not stated | Packagist API best practices: `mailto` in User-Agent, at most 10 (or 20 for static files) concurrent requests | No | **Accepted risk** (repository URL identifies the client; no e-mail is disclosed) |
| NuGet | api.nuget.org, azuresearch-usnc.nuget.org | Search results, `.nupkg` files (tool commands), catalog | Not stated for metadata; packages under each package's license | NuGet.org Terms of Use; rate limits page | No | OK |
| ConanCenter | center2.conan.io, codeload.github.com (conan-io/conan-center-index), api.github.com | Recipe index, package manifests, recipe change feed | Recipes: MIT; manifests not stated | conan.io Terms and Conditions (website); GitHub terms | No | OK; center2 terms **unverified** |
| vcpkg | codeload.github.com (microsoft/vcpkg) | Port files (`vcpkg_copy_tools`) | MIT | GitHub terms | Only for copied excerpts | OK (excerpts noticed in `NOTICE`) |
| xmake-repo | codeload.github.com (xmake-io/xmake-repo) | Package recipes (`set_kind("binary")`) | Apache-2.0 | GitHub terms | Only for copied excerpts | OK (excerpt noticed in `NOTICE`) |
| Debian | deb.debian.org | `Contents-amd64.gz` file lists | Not stated for index files | No published usage terms found | No | **Unverified** |
| Ubuntu | archive.ubuntu.com | `Contents-amd64.gz` file list (noble) | Not stated for index files | Canonical IP rights policy (trademarks, redistribution of Ubuntu) | No | **Unverified** |
| Arch Linux | geo.mirror.pkgbuild.com | `.files` databases (core, extra, multilib) | Not stated for index files | Arch Linux Terms of Service cover interactive services | No | **Unverified** |
| MSYS2 | repo.msys2.org | `.files` databases (msys, mingw64, ucrt64, clang64) | Not stated for index files | No published usage terms found | No | **Unverified** |
| Homebrew | formulae.brew.sh | `formula.json` (`executables`, aliases) | BSD-2-Clause (homebrew-core, formulae.brew.sh) | Public JSON API documentation | Only for copied content | OK |
| Scoop buckets | codeload.github.com (10 bucket repositories) | Manifest `bin` and `shortcuts` | Unlicense, MIT or CC0-1.0, per bucket | GitHub terms | Only for copied content | OK |
| winget | cdn.winget.microsoft.com | `source.msix` index (declared `Commands`) | Manifests in microsoft/winget-pkgs: MIT; the index file itself not stated | Not found for the CDN | No | **Unverified** |
| Windows container images | mcr.microsoft.com | File names on the default PATH inside Server Core and Nano Server layers | Image provisioning: Microsoft Software Supplemental License; output: factual names only | Provisioning conditions are separate from inventory | No | OK (inventory and factual state publication) |
| macOS base system | none (GitHub-hosted macOS runner) | File names in system directories of the runner | OS provisioning: Apple software license; output: factual names only | Runner provisioning is separate from inventory | No | OK (inventory and factual state publication) |
| Shell built-ins | none (shells in debian, alpine, fedora images); pubs.opengroup.org and learn.microsoft.com cited | Built-in names from `compgen -b` and equivalents; embedded lists for `sh` and `cmd` | Names only; shells under their own licenses | Docker Hub terms for the images | No | OK |
| GitHub | github.com, codeload.github.com, api.github.com, raw.githubusercontent.com | Repository archives above; compare API for ConanCenter; hosting of this project | Per repository | GitHub Terms of Service and Acceptable Use Policies | No | OK |

## Per-source notes

### ecosyste.ms Packages

- Code: `tools/npm_critical_catalog.py` (run by
  `.github/workflows/registry-artifacts.yml`). It reads
  `https://packages.ecosyste.ms/critical?registry=npmjs.org&per_page=1000`
  page by page, keeps only the package names from the links, and writes
  `data/production/npm-critical-packages.txt` (2,295 names). The file is
  committed on `main` and copied to the `artifact-data` branch. The npm crawler
  uses it as the list of npm packages to inspect, and the dataset metadata
  (`coverage.npm`) records its source URL, digest and package count.
- License: the footer of every packages.ecosyste.ms page says "Data: CC BY-SA
  4.0" (https://creativecommons.org/licenses/by-sa/4.0/). The API documentation
  (https://ecosyste.ms/api) says the APIs "are available under CC-BY-SA-4.0
  license".
- Terms: https://ecosyste.ms/terms, section 11(b), says the Data "is subject to
  a creative commons attribution share-alike license", that users must
  attribute "ecosyste.ms" as the source of data "when published alongside, or
  combined with other data", and must not indicate association with
  ecosyste.ms. Section 11(c) grants a "personal use" license to the Platform
  and excludes "any data mining, robots or similar data gathering or extraction
  methods", and section 9 prohibits "any scripts designed to scrape or extract
  data from our Platform" without prior written consent. robots.txt allows all
  paths. The same site documents a public API, including a `/api/v1/critical`
  endpoint, and a polite pool for clients that send a `mailto` address
  (https://ecosyste.ms/api). ecosyste.ms also offers commercial licenses.
- Attribution today: none in the repository before this change. This change
  adds an attribution notice for the file in `NOTICE`.
- Status: **Gap.** Three points are open:
  1. Share-alike (see below).
  2. Our tool parses HTML pages, which sits uneasily with the scraping clause
     in section 9 even though robots.txt allows it. Switching to the
     documented `/api/v1/critical` endpoint with a `mailto` in the User-Agent
     is the closer fit to what the site offers; whether automated API use is
     covered by the "personal use" wording in 11(c) is **unverified** and is a
     question for ecosyste.ms.
  3. The User-Agent has no `mailto`, so requests are served from the common
     pool.

#### What share-alike requires here

CC BY-SA 4.0 section 3(b) applies to "Adapted Material": material derived
from the licensed material in which it is "translated, altered, arranged,
transformed, or otherwise modified". Section 4(b) adds that where we have sui
generis database rights in a database that includes all or a substantial
portion of the licensed database contents, that database is Adapted Material.

- The list file, `data/production/npm-critical-packages.txt`, is a copy of
  ecosyste.ms data with formatting changes. Shared as it is, it has to carry
  attribution (creator "ecosyste.ms", a copyright notice if supplied, a link
  to the license, a link to the source, and an indication of changes, section
  3(a)), and it has to be offered under CC BY-SA 4.0 (or a BY-SA compatible
  license), with no additional terms or technological measures that restrict
  others (section 3(b)). Today it sits in a repository whose `LICENSE` is MIT
  and has no notice of its own.
- The dataset on the `dictionary` branch does not contain the list. It
  contains npm command records for those listed packages that declare a
  `bin`, read from the npm registry, plus the list's URL, digest and count in
  metadata. Whether that makes the npm part of the dataset Adapted Material is
  **unverified**: the selection of which npm packages appear comes from the
  list, but the records themselves come from npm. If it were Adapted Material,
  the npm part of the dataset (or, under section 4(b), the dataset as a whole)
  would have to be offered under CC BY-SA 4.0 with attribution. The
  `dictionary` branch has no license file today, and the README says the data
  is reusable under the repository license, which is MIT.

Options, for a decision by the maintainer (none is chosen here):

1. Keep the list. Add attribution (done in `NOTICE` by this change) and state
   that `data/production/npm-critical-packages.txt` is licensed under CC BY-SA
   4.0, separately from the MIT code; also decide and state which license the
   dataset carries, and add the same attribution where the dataset is
   published (the `dictionary` branch, the playground and `get_coverage`).
2. Replace the list with one built from a source already in use and not under
   share-alike, for example npm's own data (the registry, its replication
   feed, or download counts from api.npmjs.org, which is not used today and
   whose terms would need the same review). This changes what "critical"
   means and needs its own coverage wording.
3. Ask ecosyste.ms (support@ecosyste.ms) whether using the list as a crawl
   seed and publishing only npm-derived command records needs share-alike,
   and whether automated use of the API is permitted, or obtain a license
   that covers it.

### npm registry

- Code: `internal/registryinspect/npm.go` reads
  `https://registry.npmjs.org/<name>/latest` for the critical set and keeps
  `bin`. `tools/npm_registry_crawl.py` reads the same endpoint for a separate
  broad crawl, and `tools/npm_full_catalog.py` obtains package names from
  `https://replicate.npmjs.com/_all_docs`.
  `internal/registryinspect/feeds.go` reads
  `https://replicate.npmjs.com/_changes`, and
  `src/global_executables/live_sources.py` reads one package for a smoke test.
  No package tarball is downloaded.
- Terms: npm Open-Source Terms (https://docs.npmjs.com/policies/open-source-terms).
  Permission 2 allows searching and downloading packages "via application
  programming interfaces that npm publicly documents or makes available for
  public use"; Acceptable Use 9 allows replicating data "from the Public
  Registry using the Public APIs"; Acceptable Use 17 says five million requests
  in a month by one party is not reasonable. Condition 4 restricts sharing npm
  security data; we do not read it.
- Data license: the terms leave package content to its publishers ("You
  decide whether and how to license it") and grant no separate license for
  registry metadata.
- Volume: the critical sweep reads about 2,300 packages. The broad workflow
  budgets 10,000 package reads every four hours, with a 200 ms floor between
  requests on one runner, plus a paginated name-index read on its first run and
  every seven days thereafter. New names append to the existing crawl catalog
  so its cursor remains valid.
- Status: OK.

### PyPI

- Code: `internal/registryinspect/pypi.go` reads `/pypi/<project>/json`
  (with a Range read for projects whose JSON is over 8 MB) and downloads the
  wheel or sdist it lists from files.pythonhosted.org to read
  `entry_points.txt` and console scripts. The bounded exhaustive rotation
  revisits every project instead of using PyPI's deprecated XML-RPC changelog.
  `src/global_executables/production.py` uses `https://pypi.org/simple/`.
  Actions identifies the public repository in the User-Agent. A local booster
  may add a contact value explicitly through `CONTACT`.
- Terms: PyPI Terms of Service, effective 2025-02-25
  (https://policies.python.org/pypi.org/Terms-of-Service/), API Terms:
  abuse or excessively frequent requests may lead to suspension. API policies
  (https://docs.pypi.org/api/): no edge rate limit; set a unique User-Agent
  with contact information; spread requests out; "No new integrations should
  use the XML-RPC APIs as they are planned for deprecation."
- Data license: uploaded content keeps its own license; the terms grant PSF
  and users a license to reproduce and distribute content uploaded without a
  license. No separate license is stated for metadata.
- Status: OK. The crawler uses the JSON and Simple APIs plus artifact files;
  it does not use the deprecated XML-RPC API. Its Actions User-Agent uniquely
  identifies the public repository; PyPI describes contact information as
  helpful rather than required, so Actions does not disclose an e-mail address.

### crates.io

- Code: `src/global_executables/registry_artifact.py` downloads
  `https://static.crates.io/db-dump.tar.gz` and reads `bin_names` per version.
  `production.py` names `https://index.crates.io/config.json`, and
  `live_sources.py` calls `crates.io/api/v1` twice for a smoke test.
- Terms: crates.io Data Access Policy (https://crates.io/data-access) points
  bulk users to the database dump; the API is limited to 1 request per second
  with an identifying User-Agent.
- Data license: the README inside the dump (checked on the 2026-10-19 dump)
  states no license; it says the CSV files contain metadata "exactly as it
  was submitted".
- Status: OK.

### Go module mirror, index and pkg.go.dev

- Code: `internal/goproxy/` reads `index.golang.org/index`, and from
  `proxy.golang.org` the `@v/list`, `@latest`, `.mod` and `.zip` of each
  module, using `https://proxy.golang.org/cached-only` for archives first. It
  reads `package main` declarations from the source files in the zip and
  publishes command names only. `internal/goproxy/package_index.go` calls
  `https://pkg.go.dev/v1/packages/<module>` with at least 25 ms between calls.
- Terms: https://proxy.golang.org/ says users "downloading large numbers of
  modules (e.g. for bulk static analysis)" should use `/cached-only`, which we
  do. pkg.go.dev API documentation (https://pkg.go.dev/v1/api) sets 45 queries
  per second per IP block; our interval keeps us at or under 40. The Google
  terms that govern pkg.go.dev were not checked (**unverified**).
- Data license: module contents are under each module's license; the proxy
  only keeps modules long-term when it detects a suitable license.
- Status: OK.

### RubyGems.org

- Code: `internal/registryinspect/rubygems.go` reads
  `/api/v1/gems/<name>.json` and downloads `/gems/<name>-<version>.gem` to read
  `executables`.
- Terms: RubyGems.org Terms of Service (https://rubygems.org/policies/terms-of-service),
  section 3(d): users of the service get a license "to download and reproduce"
  content; other use follows each gem's license. Rate limits
  (https://guides.rubygems.org/rubygems-org-rate-limits/): API and website 10
  requests per second; the team may block IPs in extreme cases.
- Status: OK. The crawler keeps its 16 workers for artifact processing, but a
  process-wide 100 ms request interval gates every RubyGems HTTP request and
  keeps aggregate throughput at or below 10 requests per second.

### Packagist

- Code: `internal/registryinspect/packagist.go` reads
  `https://repo.packagist.org/p2/<vendor>/<package>.json` and keeps `bin`;
  `internal/registryinspect/feeds.go` reads
  `https://packagist.org/metadata/changes.json`.
- Terms: API best practices (https://packagist.org/apidoc): send a User-Agent
  that includes `mailto=you@example.com`; at most 10 concurrent requests, or
  up to 20 if only fetching static files; avoid running scheduled jobs on the
  hour.
- Status: **Accepted risk.** The crawler defaults to 20 workers and the
  schedule (`17 */6 * * *`) avoids the top of the hour. Its User-Agent links
  to the public repository, but deliberately does not disclose an operator
  e-mail address. This does not follow Packagist's `mailto` recommendation and
  may make blocking more likely if Packagist needs to contact the operator.

### NuGet

- Code: `src/global_executables/registry_artifact.py` queries
  `https://azuresearch-usnc.nuget.org/query`, downloads packages from
  `https://api.nuget.org/v3-flatcontainer` to read tool command names, and
  `src/global_executables/change_feeds.py` reads
  `https://api.nuget.org/v3/catalog0/index.json`.
- Terms: Microsoft Visual Studio Marketplace and NuGet.org Terms of Use
  (https://www.nuget.org/policies/Terms), section 3: obtaining material
  "through any means other than directly from Microsoft's publicly supported
  interfaces" is prohibited. The search host we use is listed as a
  `SearchQueryService` in the service index (https://api.nuget.org/v3/index.json),
  and the V3 search API is not rate limited
  (https://learn.microsoft.com/nuget/api/rate-limits).
- Status: OK. The search URL is hard-coded rather than read from the service
  index; reading it from the index would follow NuGet's documented client
  behaviour if the host changes.

### ConanCenter

- Code: `src/global_executables/registry_artifact.py` downloads the
  conan-center-index archive from codeload.github.com and reads package
  manifests from `https://center2.conan.io/v2/conans`;
  `src/global_executables/change_feeds.py` uses the GitHub compare API on
  `conan-io/conan-center-index`.
- License: conan-center-index is MIT. No license is stated for the manifests
  served by center2.
- Terms: https://conan.io/terms-conditions covers the conan.io website; we
  found no terms specific to the center2 remote (**unverified**).
- Status: OK; center2 terms unverified.

### vcpkg and xmake-repo

- Code: `production.py` downloads the archives of `microsoft/vcpkg` (MIT) and
  `xmake-io/xmake-repo` (Apache-2.0) from codeload.github.com and parses
  recipes for installed tools.
- Two vcpkg port files and one xmake-repo recipe are excerpted in
  `fixtures/collectors/` and ship in the source distribution. Their license
  notices were missing; this change adds them to `NOTICE` and adds
  `LICENSES/Apache-2.0.txt`.
- Status: OK after this change.

### Debian and Ubuntu

- Code: `production.py` downloads `Contents-amd64.gz` for Debian stable
  (main, contrib, non-free, non-free-firmware) and sid main from
  deb.debian.org, and for Ubuntu noble from archive.ubuntu.com.
- Terms: Debian publishes no usage terms for archive index files that we
  could find (https://www.debian.org/legal/). Canonical's IP rights policy
  (https://ubuntu.com/legal/intellectual-property-policy) covers trademarks and
  redistribution of Ubuntu; we use the name "ubuntu" only to label the source.
- Status: **Unverified** (no terms found). These files are published for
  package managers to download in full.

### Arch Linux and MSYS2

- Code: `production.py` downloads the `.files` databases of Arch core, extra
  and multilib from geo.mirror.pkgbuild.com and of four MSYS2 repositories
  from repo.msys2.org.
- Terms: the Arch Linux Terms of Service (https://terms.archlinux.org/docs/terms-of-service/)
  cover registered, interactive services and say nothing about mirror
  downloads. We found no terms for repo.msys2.org.
- Status: **Unverified** (no terms found).

### Homebrew

- Code: `production.py` downloads `https://formulae.brew.sh/api/formula.json`
  and keeps `executables` and aliases; `live_sources.py` reads one formula.
- License: Homebrew/homebrew-core and Homebrew/formulae.brew.sh are
  BSD-2-Clause. The API is documented for public use
  (https://formulae.brew.sh/docs/api/).
- Status: OK.

### Scoop buckets

- Code: `production.py` downloads the archives of ScoopInstaller/Main,
  Extras, Versions, Nirsoft, PHP, Java and Nonportable,
  niheaven/scoop-sysinternals, matthewjberger/scoop-nerd-fonts and
  Calinou/scoop-games from codeload.github.com.
- License: Unlicense (Main, Extras, Versions, Java, Nonportable,
  scoop-nerd-fonts), MIT (Nirsoft, PHP, scoop-sysinternals), CC0-1.0
  (scoop-games), as reported by GitHub.
- Status: OK.

### winget

- Code: `production.py` downloads `https://cdn.winget.microsoft.com/cache/source.msix`
  and reads declared `Commands`.
- License: the manifests come from microsoft/winget-pkgs, which is MIT. We
  found no license or terms for the CDN index file itself (**unverified**).
- Status: **Unverified.**

### Windows container base images

- Code: `production.py` reads manifests and layer blobs of
  `windows/servercore` and `windows/nanoserver` (ltsc2022 and ltsc2025) from
  `https://mcr.microsoft.com/v2`, lists file names under the default PATH
  directories, and pins the image digest. Nothing is executed and no file
  content is kept.
- Acquisition reference: Microsoft Software Supplemental License for Windows
  Container Base Image
  (https://mcr.microsoft.com/en-us/artifact/mar/windows/servercore/about).
  This is a reference for image provisioning and runtime conditions. It does
  not turn the separate act of listing installed package/file names and
  publishing factual observations into redistribution of an image or package.
- Status: OK for inventory observation and factual state publication. The
  collector keeps names and the image digest, not image/package contents.
  Provisioning questions must be assessed separately for the actual acquisition
  and runtime workflow; this page does not infer an inventory rights gap from
  the collector's host OS.

### macOS base system

- Code: `.github/workflows/base-commands.yml` runs
  `tools/production_crawl.py --source macos` on a GitHub-hosted macOS runner
  and lists file names in system directories, pinned to the OS build.
- Scope: the collector observes file names on the existing runner and
  publishes factual state. It does not distribute macOS or package contents.
  The runner's OS provisioning is a separate workflow.
- Status: OK for local inventory observation and factual state publication.

### Shell built-ins

- Code: `production.py` asks bash, zsh, fish and ksh for their built-in names
  (`compgen -b` and equivalents) inside `debian:stable-slim`, `alpine:latest`
  and `fedora:latest` containers and on the macOS runner. For `sh` and `cmd`,
  which cannot be asked, it embeds name lists taken from the POSIX.1-2017
  utilities index (https://pubs.opengroup.org/onlinepubs/9699919799/idx/utilities.html)
  and Microsoft's `cmd` reference
  (https://learn.microsoft.com/windows-server/administration/windows-commands/cmd).
- These are lists of command names only.
- Status: OK.

### GitHub

- Hosts: github.com (this repository, workflows, release links),
  codeload.github.com (repository archives above, and the `dictionary`
  archive in `Dockerfile`), api.github.com (ConanCenter compare API, live
  smoke test), raw.githubusercontent.com (the playground, `remote.py` and the
  OpenAPI description read the published `dictionary` branch of this
  repository).
- Terms: GitHub Terms of Service and Acceptable Use Policies
  (https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies):
  no excessive automated bulk activity; API use follows the API terms and
  rate limits.
- Status: OK.

## Hosts used for build and publishing only

These hosts do not supply data for the dataset.

- asopitech-labs.github.io: this project's playground and homepage.
- static.modelcontextprotocol.io: JSON schema referenced by `server.json`.
- ghcr.io and Docker Hub (`python`, `golang`, `node`, `debian`, `alpine`,
  `fedora` images): container images for builds, tests and the MCP image.
- pypi.org (upload): the release workflow publishes this package.
- json-schema.org: schema identifiers in `schema/`.

## Gaps found

1. ecosyste.ms: share-alike for the npm critical list (and possibly the npm
   part of the dataset) is undecided; the list is gathered by parsing HTML
   pages, which the ecosyste.ms terms restrict; no `mailto` is sent.
   Attribution is added by this change.
2. Dataset license: the `dictionary` branch carries no license file, and the
   README points to the repository's MIT license. The dataset's license needs
   to be stated, and the choice depends on item 1.
3. Packagist: #110 reduced default concurrency to 20. The maintainer chose not
   to disclose an e-mail address in automated requests; the public repository
   URL remains in the User-Agent. This is an accepted risk rather than full
   compliance with Packagist's `mailto` recommendation.
4. RubyGems.org: process-wide request pacing was added in #110 to enforce the
   10 requests per second API limit.
5. PyPI: #110 removed the deprecated XML-RPC change feed; bounded exhaustive
   rotation now discovers updates.
6. Test fixtures with vcpkg and xmake-repo excerpts had no license notices.
   Fixed by this change.
7. Unverified external acquisition references: terms for Debian, Ubuntu, Arch and MSYS2 archive files, the
   winget CDN index, ConanCenter's center2 remote, and pkg.go.dev's terms of
   service. These are not findings against local inventory or factual state
   publication.
