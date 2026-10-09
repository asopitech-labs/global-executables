import pytest

from global_executables import registry_artifact


@pytest.fixture(autouse=True)
def offline_change_feeds(monkeypatch):
    """No test reaches a registry change feed; feed tests install their own transport."""
    def refuse(url, *args, **kwargs):
        raise OSError(f"network access to {url} is not allowed in tests")

    monkeypatch.setattr(registry_artifact, "_conan_feed_request", refuse)
    monkeypatch.setattr(registry_artifact, "_nuget_feed_request", refuse)
