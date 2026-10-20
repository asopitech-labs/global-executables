# Installation guidance validation

The `installation-v1` resolver separates three facts: the indexed package-to-command
mapping, whether the generated argv works for a package-manager method, and whether
that exact provider/version works on the caller's target. A successful method probe
never upgrades a provider to `ready` by itself.

| Method | Disposable target | Observed at (UTC) | Result | Scope |
| --- | --- | --- | --- | --- |
| npm project | Debian 13 amd64; Node.js 24.19.0; npm 11.9.0 | 2026-10-19 10:45 | pass | A local `ge-install-probe@1.0.0` package with a `bin` entry was installed with `npm install --prefix <tmp>/project --offline --ignore-scripts --no-audit --no-fund -- <tmp>/package`; from the project directory, `npm exec --offline -- ge-install-probe` printed `GE_INSTALL_PROBE_OK`. |
| pip project | Debian 13 amd64; Python 3.12.14 | 2026-10-19 10:46 | pass | A local `ge-install-probe==1.0.0` project with a `project.scripts` entry was installed into a temporary venv via `<venv>/bin/python -m pip install --no-index --no-build-isolation -- <tmp>/package`; `<venv>/bin/ge-install-probe` printed `GE_INSTALL_PROBE_OK`. |
| apt | Debian 13 amd64; apt 3.0.3 | 2026-10-19 10:46 | unavailable | `apt-cache policy` could not read `/etc/apt/apt.conf.d/80-applied-apt-retries` (permission denied). No install-and-invoke probe completed. |
| Homebrew | macOS target unavailable | 2026-10-19 10:46 | unavailable | No macOS target was available. |

The npm probe used a local package path rather than a registry package identity. The
pip probe used a local project and a temporary venv rather than a published PyPI
identity. Therefore neither probe validates the indexed packages, their stated
versions, repository state, architecture, or platform compatibility. The resolver
returns `unverified` for these cases. Apt additionally requires a matching source
release; currently indexed apt providers without release provenance stay
`unverified`.
