"""Private SPA/API responses discourage caches and crawlers; static assets retain normal behavior."""


def test_direct_private_spa_api_headers_and_assets(client_a, tmp_path, monkeypatch):
    import app.main as main

    page = tmp_path / "index.html"
    page.write_text("<!doctype html><html><body>Synthetic app shell</body></html>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "test.js").write_text('console.log("synthetic");')
    monkeypatch.setattr(main, "SPA_DIST", tmp_path)
    monkeypatch.setattr(main, "SPA_INDEX", page)
    for route in ["/", "/login", "/coach", "/plans", "/plans/synthetic", "/settings", "/api/plans", "/api/health"]:
        response = client_a.get(route)
        assert response.status_code == 200
        assert "no-store" in response.headers.get("Cache-Control", "")
        assert "private" in response.headers.get("Cache-Control", "")
        assert "authorization" in response.headers.get("Vary", "").lower()
        assert response.headers.get("X-Robots-Tag") == "noindex, nofollow"
    script = client_a.get("/assets/test.js")
    assert script.status_code == 200
    assert "no-store" not in script.headers.get("Cache-Control", "")
    assert "X-Robots-Tag" not in script.headers
