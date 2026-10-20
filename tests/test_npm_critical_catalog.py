import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "tools/npm_critical_catalog.py"
spec = importlib.util.spec_from_file_location("npm_critical_catalog", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_page_extracts_unique_packages_and_next_page():
    names, next_url, last_page = module.parse_page(
        """
        <a href="/registries/npmjs.org/packages/foo">foo</a>
        <a href="/registries/npmjs.org/packages/%40scope%2Ftool">tool</a>
        <a href="/registries/npmjs.org/packages/foo">foo again</a>
        <a href="/critical?registry=npmjs.org&amp;per_page=1000&amp;page=2"
           aria-label="Next">next</a>
        <a href="/critical?registry=npmjs.org&amp;per_page=1000&amp;page=3">3</a>
        """,
        "https://packages.ecosyste.ms/critical?registry=npmjs.org",
    )
    assert names == {"foo", "@scope/tool"}
    assert next_url == (
        "https://packages.ecosyste.ms/critical?registry=npmjs.org"
        "&per_page=1000&page=2"
    )
    assert last_page == 3


def packages(start, stop):
    return "".join(
        f'<a href="/registries/npmjs.org/packages/package-{number}">package</a>'
        for number in range(start, stop)
    )


def test_fetch_rejects_a_full_page_without_complete_pagination(monkeypatch):
    body = packages(0, 1000)

    class Response:
        url = module.START_URL

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return body.encode()

    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError, match="pagination is missing"):
        module.fetch_catalog()


def test_fetch_rejects_missing_pagination_after_a_full_later_page(monkeypatch):
    bodies = iter([
        packages(0, 1000) + '<a href="?registry=npmjs.org&amp;per_page=1000&amp;page=2" aria-label="Next">next</a>',
        packages(1000, 2000),
    ])

    class Response:
        def __init__(self, request):
            self.url = request.full_url
            self.body = next(bodies)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return self.body.encode()

    monkeypatch.setattr(module, "urlopen", lambda request, **_kwargs: Response(request))

    with pytest.raises(RuntimeError, match="pagination is missing"):
        module.fetch_catalog()
