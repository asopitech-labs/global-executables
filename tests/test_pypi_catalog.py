import gzip
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

from global_executables.registry_state import load_state, save_state


SCRIPT = Path(__file__).parents[1] / "tools/pypi_catalog.py"
spec = importlib.util.spec_from_file_location("pypi_catalog", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response:
    status = 200

    def __init__(self, payload, *, etag='"next"'):
        self.payload = json.dumps(payload).encode()
        self.headers = Message()
        self.headers["ETag"] = etag

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, *_args):
        return self.payload


def payload(*names, serial=123):
    return {"meta": {"_last-serial": serial}, "projects": [{"name": name} for name in names]}


def setup_catalog(tmp_path, names=("Existing_Name", "keep"), **state_values):
    catalog = tmp_path / "pypi-projects.txt"
    companion = tmp_path / "pypi-projects.txt.gz"
    catalog.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
    companion.write_bytes(gzip.compress(catalog.read_bytes(), mtime=0))
    state = tmp_path / "registry-state"
    save_state(state, {"version": 1, "sources": {"pypi": {"cursor": len(names), **state_values}}})
    return catalog, companion, state


def test_refresh_appends_only_new_normalized_names_and_stores_metadata(tmp_path):
    catalog, companion, state = setup_catalog(tmp_path, ("Existing_Name", "keep", "Y-old", "Z-old"))
    now = datetime(2026, 11, 28, 1, 2, 3, tzinfo=timezone.utc)
    requests = []

    def open_response(request, *, timeout):
        requests.append((request, timeout))
        return Response(
            payload("existing-name", "New.Zeta", "Z-old", "keep", "New.Alpha", "Y-old", serial=44_281_061)
        )

    result = module.refresh_catalog(
        catalog, companion, state, opener=open_response, now=now, contact="ops@example.test"
    )

    assert result.added == 2 and result.missing == 0
    assert catalog.read_text(encoding="utf-8") == "Existing_Name\nkeep\nY-old\nZ-old\nNew.Alpha\nNew.Zeta\n"
    assert gzip.decompress(companion.read_bytes()) == catalog.read_bytes()
    entry = load_state(state)["sources"]["pypi"]
    assert entry["catalog_size"] == 6
    assert entry["catalog_etag"] == '"next"'
    assert entry["catalog_fetched_at"] == "2026-11-28T01:02:03Z"
    assert entry["catalog_serial"] == 44_281_061
    assert entry["catalog_live_size"] == 6 and entry["catalog_missing"] == 0
    assert entry["catalog_refresh_error"] == ""
    request, timeout = requests[0]
    assert request.headers["Accept"] == "application/vnd.pypi.simple.v1+json"
    assert "global-executables" in request.headers["User-agent"]
    assert "ops@example.test" in request.headers["User-agent"] and timeout == 120


def test_not_modified_does_not_rewrite_catalog_and_defers_the_next_fetch(tmp_path):
    catalog, companion, state = setup_catalog(tmp_path, catalog_etag='"old"')
    before = (catalog.read_bytes(), companion.read_bytes(), catalog.stat().st_mtime_ns, companion.stat().st_mtime_ns)
    now = datetime(2026, 11, 28, tzinfo=timezone.utc)
    calls = []

    def not_modified(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 304, "Not Modified", Message(), None)

    result = module.refresh_catalog(catalog, companion, state, opener=not_modified, now=now)
    again = module.refresh_catalog(
        catalog, companion, state, opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fetched twice")),
        now=now + timedelta(hours=23, minutes=59),
    )

    assert result.not_modified and again.skipped
    assert (catalog.read_bytes(), companion.read_bytes(), catalog.stat().st_mtime_ns, companion.stat().st_mtime_ns) == before
    assert calls[0].headers["If-none-match"] == '"old"'
    assert load_state(state)["sources"]["pypi"]["catalog_fetched_at"] == "2026-11-28T00:00:00Z"


def test_refresh_rejects_shrink_malformed_and_growth_without_touching_catalog(tmp_path):
    cases = [
        (payload(*[f"name-{index}" for index in range(94)]), "shrunk"),
        ({"meta": {"_last-serial": 1}, "projects": [{"not-name": "x"}]}, "malformed"),
        (payload(*[f"name-{index}" for index in range(151)]), "growth"),
    ]
    for index, (body, error) in enumerate(cases):
        directory = tmp_path / str(index)
        directory.mkdir()
        catalog, companion, state = setup_catalog(directory, tuple(f"name-{number}" for number in range(100)))
        before = (catalog.read_bytes(), companion.read_bytes())
        result = module.refresh_catalog(
            catalog,
            companion,
            state,
            opener=lambda *_args, body=body, **_kwargs: Response(body),
            now=datetime(2026, 11, 28, index, tzinfo=timezone.utc),
        )
        assert result.error and error in result.error.lower()
        assert (catalog.read_bytes(), companion.read_bytes()) == before
        entry = load_state(state)["sources"]["pypi"]
        assert entry["catalog_size"] == 100 and entry["catalog_refresh_error"] == result.error


def test_refresh_rejects_duplicate_normalized_project_names(tmp_path):
    catalog, companion, state = setup_catalog(tmp_path, ("one",))
    before = catalog.read_bytes()
    result = module.refresh_catalog(
        catalog,
        companion,
        state,
        opener=lambda *_args, **_kwargs: Response(payload("one", "New_Name", "new-name")),
        now=datetime(2026, 11, 28, tzinfo=timezone.utc),
    )
    assert "duplicate normalized" in result.error.lower()
    assert catalog.read_bytes() == before
