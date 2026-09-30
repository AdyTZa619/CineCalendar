import requests

from cinecalendar.collaborative_als import CollaborativeALSProvider


def test_cached_model_can_use_bundled_manifest_when_github_times_out(monkeypatch):
    def offline(*_args, **_kwargs):
        raise requests.Timeout("GitHub unavailable")

    monkeypatch.setattr(requests, "get", offline)
    manifest = CollaborativeALSProvider._fetch_manifest()
    assert manifest["version"] == "ml32m-als-v1"
    assert len(manifest["sha256"]) == 64
