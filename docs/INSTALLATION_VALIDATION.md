# Installation guidance validation

The `installation-v1` resolver separates three facts: the indexed package-to-command
mapping, whether the generated argv works for a package-manager method, and whether
that exact provider/version works on the caller's target. A successful method probe
never upgrades a provider to `ready` by itself.

## Earlier local fixture checks

| Method | Disposable target | Observed at (UTC) | Result | Scope |
| --- | --- | --- | --- | --- |
| npm project | Debian 13 amd64; Node.js 24.19.0; npm 11.9.0 | 2026-10-19 10:45 | pass | A local `ge-install-probe@1.0.0` package with a `bin` entry was installed with `npm install --prefix <tmp>/project --offline --ignore-scripts --no-audit --no-fund -- <tmp>/package`; from the project directory, `npm exec --offline -- ge-install-probe` printed `GE_INSTALL_PROBE_OK`. |
| pip project | Debian 13 amd64; Python 3.12.14 | 2026-10-19 10:46 | pass | A local `ge-install-probe==1.0.0` project with a `project.scripts` entry was installed into a temporary venv via `<venv>/bin/python -m pip install --no-index --no-build-isolation -- <tmp>/package`; `<venv>/bin/ge-install-probe` printed `GE_INSTALL_PROBE_OK`. |
| apt local `.deb` | Debian 13 amd64; apt 3.0.3 | 2026-10-20 00:58 | pass | Built a disposable `ge-install-probe` Debian package, installed it with `apt-get install <local.deb>` into an isolated writable root using custom apt/dpkg state, and invoked its executable (`GE_INSTALL_PROBE_OK`). apt emitted sandbox configuration warnings but exited 0 and dpkg recorded `install ok installed`. Repository availability for an indexed package was not tested. |
| Homebrew | macOS target unavailable | 2026-10-19 10:46 | unavailable | No macOS target was available. |

These fixture probes do not validate indexed packages, their stated versions,
repository state, architecture, or platform compatibility. The resolver returns
`unverified` for those cases. Apt additionally requires matching source-release
evidence and adds an availability-check step when that evidence is missing.

## Published package checks

[Verify installation methods run 39362250172](https://github.com/asopitech-labs/global-executables/actions/runs/39362250172)
installed and invoked public packages on hosted targets. The jobs ran on
Ubuntu 24.04, Debian 13, and macOS 15; all three passed.

| Method | Published package and target | Install and invoke result |
| --- | --- | --- |
| apt | `hello` on Ubuntu 24.04 | `apt-get install --reinstall --no-install-recommends hello` succeeded, `dpkg-query` reported `install ok installed`, and `hello` printed `Hello, world!`. |
| apt | `hello` on Debian 13 | A fresh Debian container ran `apt-get install --no-install-recommends hello`; `dpkg-query` reported `install ok installed` and `hello` printed `Hello, world!`. |
| npm project | `cowsay@1.6.0` on Ubuntu 24.04 with Node.js 24 | `npm install --prefix <project> -- cowsay@1.6.0` succeeded, and `npm exec -- cowsay INSTALL_OK` printed `INSTALL_OK`. Registry metadata declares `cowsay` as a binary entry point. |
| npm user | `cowsay@1.6.0` on Ubuntu 24.04 with Node.js 24 | `npm install --global -- cowsay@1.6.0` succeeded with `NPM_CONFIG_PREFIX` set to a disposable user directory, and the installed binary printed `INSTALL_OK`. |
| pip project | `black==25.9.0` on Ubuntu 24.04 with Python 3.12 | A venv's Python installed the published wheel and its `black --version` command succeeded. |
| pip user | `black==25.9.0` on Ubuntu 24.04 with Python 3.12 | `python -m pip install --user black==25.9.0` succeeded with `PYTHONUSERBASE` set to a disposable user directory, and its `black --version` command succeeded. |
| Homebrew | `hello` formula on macOS 15 | `brew install hello` (or `brew reinstall hello` if already present) succeeded and the formula's `hello` binary printed `Hello, world!`. |

These public-package checks prove that the supported package-manager commands can install and
invoke these specific public examples on the stated targets. They do not validate
every indexed provider or turn a different provider's `unverified` result into
`ready`. The workflow can be dispatched again after an environment or manager
change. `unverified` describes the selected package on the target, not whether
the package manager has a documented installation command.
