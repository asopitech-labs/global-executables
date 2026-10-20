"""Conservative, read-only installation guidance over observed provider records."""
from __future__ import annotations

from typing import Any

from .model import valid_command

CONTRACT_VERSION = "installation-v1"
SUPPORTED = {"homebrew": "brew", "debian": "apt", "ubuntu": "apt", "npm": "npm", "pypi": "pip"}
METHOD_DOCS = {
    "apt": "https://manpages.debian.org/apt-get.8",
    "brew": "https://docs.brew.sh/Manpage#install-options-formulacask-",
    "npm": "https://docs.npmjs.com/cli/commands/npm-install",
    "pip": "https://pip.pypa.io/en/stable/cli/pip_install/",
}
METHOD_PROBES = {
    ("npm", "project"): {"result": "pass", "observed_at": "2026-10-19T10:45:00Z",
                          "target": "Debian 13 amd64", "manager_version": "npm 11.9.0",
                          "runtime_version": "Node.js 24.19.0", "fixture": "local ge-install-probe 1.0.0",
                          "invocation": "npm exec -- ge-install-probe"},
    ("pip", "project"): {"result": "pass", "observed_at": "2026-10-19T10:46:00Z",
                          "target": "Debian 13 amd64", "manager_version": "pip (Python 3.12.14)",
                          "runtime_version": "Python 3.12.14", "fixture": "local ge-install-probe 1.0.0",
                          "invocation": "venv/bin/ge-install-probe"},
}


def resolve_installation(dataset: Any, name: str, environment: dict[str, Any],
                         preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    if not valid_command(name):
        raise ValueError("invalid executable name: expected 1–255 characters without path separators or controls")
    if not isinstance(environment, dict) or (preferences is not None and not isinstance(preferences, dict)):
        raise ValueError("environment and preferences must be objects")
    preferences = preferences or {}
    checked = dataset.check(name)
    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION, "name": name, "status": "needs_context",
        "environment": environment, "snapshot": checked["snapshot"],
        "coverage_scope": checked["coverage_scope"], "warnings": checked["warnings"],
        "required_context": [], "candidates": [], "steps": [],
    }
    if not checked["found"]:
        result["status"] = "not_found_in_index"
        result["absence"] = checked["absence"]
        return result
    system = environment.get("package_manager")
    if not system:
        result["required_context"] = ["package_manager"]
        return result
    if system not in SUPPORTED.values():
        result["status"] = "unsupported"
        return result
    distribution = environment.get("distribution")
    if system == "apt" and not distribution:
        result["required_context"] = ["distribution"]
        return result
    if system == "apt" and distribution not in {"debian", "ubuntu"}:
        result["status"] = "unsupported"
        return result
    if system == "apt" and not environment.get("distribution_release"):
        result["required_context"] = ["distribution_release"]
        return result
    if system == "apt" and not environment.get("architecture"):
        result["required_context"] = ["architecture"]
        return result
    if system == "brew" and environment.get("os") != "macos":
        result["required_context"] = ["os"] if not environment.get("os") else []
        result["status"] = "needs_context" if result["required_context"] else "unsupported"
        return result
    if system in {"npm", "pip"} and not environment.get("runtime_version"):
        result["required_context"] = ["runtime_version"]
        return result
    if system in {"npm", "pip"} and not environment.get("install_scope"):
        result["required_context"] = ["install_scope"]
        return result
    if system in {"npm", "pip"} and environment["install_scope"] not in {"user", "project"}:
        result["status"] = "unsupported"
        return result
    ecosystem = {"brew": "homebrew", "npm": "npm", "pip": "pypi"}.get(system, distribution)
    candidates = [p for p in checked["providers"] if p["ecosystem"] == ecosystem]
    if system == "apt":
        candidates = [p for p in candidates if p.get("distribution") in {None, distribution}
                      and p.get("package_system") in {None, "deb", "apt"}]
    candidates = [p for p in candidates if not preferences.get("package") or p["package"] == preferences["package"]]
    result["candidates"] = candidates
    if not candidates:
        result["status"] = "unsupported"
        return result
    identities = {(p["ecosystem"], p["package"]) for p in candidates}
    if len(identities) > 1:
        result["status"] = "ambiguous"
        return result
    provider = candidates[0]
    result["provider"] = provider
    result["applicability"] = {"target": environment, "source_distribution_release": provider.get("distribution_release"),
                               "source_architecture": provider.get("architecture"),
                               "validated_on_target": False}
    release_matches = system != "apt" or provider.get("distribution_release") == environment["distribution_release"]
    if provider["confidence"] not in {"direct", "filesystem"}:
        result["status"] = "unverified"
        return result
    if provider.get("alias_of"):
        result["status"] = "unverified"
        return result
    package = provider["package"]
    if package.startswith("-"):
        result["status"] = "unverified"
        return result
    if system == "apt":
        if environment.get("has_admin") is not True:
            result["required_context"] = ["has_admin"]
            return result
        argv = ["sudo", "apt-get", "install", "--", package]
    elif system == "brew":
        argv = ["brew", "install", "--", package]
    elif system == "npm":
        argv = ["npm", "install", "--", package] if environment["install_scope"] == "project" else ["npm", "install", "--global", "--", package]
    else:
        if environment["install_scope"] == "project" and not environment.get("virtualenv_bin"):
            result["required_context"] = ["virtualenv_bin"]
            return result
        python = str(environment["virtualenv_bin"]).rstrip("/") + "/python" if environment["install_scope"] == "project" else "python"
        argv = [python, "-m", "pip", "install", "--", package] if environment["install_scope"] == "project" else [python, "-m", "pip", "install", "--user", "--", package]
    invoke = ["npm", "exec", "--", name] if system == "npm" and environment["install_scope"] == "project" else ([str(environment["virtualenv_bin"]).rstrip("/") + "/" + name] if system == "pip" and environment["install_scope"] == "project" else [name])
    result["status"] = "unverified"
    result["method_validation"] = METHOD_PROBES.get((system, environment.get("install_scope")))
    result["method"] = {"package_manager": system, "documentation": METHOD_DOCS[system],
                        "basis": "package-manager command syntax", "target_package_available": "unknown"}
    result["steps"] = ([{"action": "check_availability", "argv": ["apt-cache", "show", "--", package], "platform": environment}]
                       if system == "apt" and not release_matches else []) + [
        {"action": "install", "argv": argv, "platform": environment},
        {"action": "invoke", "argv": invoke, "platform": environment}]
    result["invocation"] = {"argv": invoke, "path_note": "Ensure the selected package manager's executable directory is on PATH; verify the installed command before use."}
    result["verification"] = {"confidence": provider["confidence"], "source": provider["source"],
                              "observed_at": provider.get("last_observed_at"), "version": provider.get("version"),
                              "note": "Package-manager syntax is documented; availability and entry-point behavior of this package on the target have not been verified.",
                              "source_release_matches_target": release_matches}
    return result
