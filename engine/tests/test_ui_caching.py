"""The built UI's caching: after an update, WebView2 kept showing the old build's page,
cached heuristically by its Last-Modified (user-reported). The page must be checked with
the app every load; only content-hashed assets may be kept."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import _UIFiles


def test_the_page_is_never_reused_and_hashed_assets_are(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<script src="/assets/index-abc123.js"></script>', encoding="utf-8")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "icon.svg").write_text("<svg/>", encoding="utf-8")
    app = FastAPI()
    app.mount("/", _UIFiles(directory=tmp_path, html=True), name="ui")
    client = TestClient(app)

    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/index.html").headers["cache-control"] == "no-cache"
    assert client.get("/icon.svg").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
