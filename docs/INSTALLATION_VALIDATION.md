# Installation guidance validation

The `installation-v1` resolver separates three facts: the indexed package-to-command
mapping, whether the generated argv works for a package-manager method, and whether
that exact provider/version works on the caller's target. A successful method probe
never upgrades a provider to `ready` by itself.

| Method | Disposable target | Observed at (UTC) | Result | Scope |
| --- | --- | --- | --- | --- |
| npm project | Debian 13 amd64; Node.js 24.19.0; npm 11.9.0 | 2026-10-19 10:45 | pass | A local `ge-install-probe@1.0.0` package with a `bin` entry was installed with `npm install --prefix <tmp>/project --offline --ignore-scripts --no-audit --no-fund -- <tmp>/package`; from the project directory, `npm exec --offline -- ge-install-probe` printed `GE_INSTALL_PROBE_OK`. |
| pip project | Debian 13 amd64; Python 3.12.14 | 2026-10-19 10:46 | pass | A local `ge-install-probe==1.0.0` project with a `project.scripts` entry was installed into a temporary venv via `<venv>/bin/python -m pip install --no-index --no-build-isolation -- <tmp>/package`; `<venv>/bin/ge-install-probe` printed `GE_INSTALL_PROBE_OK`. |
| apt local `.deb` | Debian 13 amd64; apt 3.0.3 | 2026-10-20 00:58 | pass | Built a disposable `ge-install-probe` Debian package, installed it with `apt-get install <local.deb>` into an isolated writable root using custom apt/dpkg state, and invoked its executable (`GE_INSTALL_PROBE_OK`). apt emitted sandbox configuration warnings but exited 0 and dpkg recorded `install ok installed`. Repository availability for an indexed package was not tested. |
| Homebrew | macOS target unavailable | 2026-10-19 10:46 | unavailable | No macOS target was available. |

The npm probe used a local package path rather than a registry package identity. The
pip probe used a local project and a temporary venv rather than a published PyPI
identity. The apt probe used a local `.deb`, not a configured Debian repository.
Therefore none of these probes validates the indexed packages, their stated
versions, repository state, architecture, or platform compatibility. The resolver
returns `unverified` for these cases. Apt additionally requires a matching source
release to confirm package availability. It still shows the documented apt method
and an availability check when release provenance is missing. `unverified` describes
the selected package on the target, not whether the package manager has a documented
installation command.
