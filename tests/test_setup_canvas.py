"""Windows consumes Canvas's one app; the service URL owns its browser prefix."""
import json
from boring_pondnt import setup_services as setup
from boring_canvas.server import App, build_parser
from boring_canvas.storage import CanvasStore
from boring_service_kit.http import ServiceConfig


def test_setup_canvas_arguments_reach_the_real_canvas_parser(tmp_path):
    result = setup.plan({}, "C:/release", str(tmp_path), "", ["canvas"],
                        "Robin", "BOX", apply=True)
    item = next(s for s in result["services"] if s["name"] == "boring-canvas")
    parsed = build_parser().parse_args(item["arguments"])
    assert str(parsed.config) == str(tmp_path / "services/canvas/config.json")
    assert not (tmp_path / "services/canvas/web.json").exists()
    assert "web_port" not in item
    config = ServiceConfig(parsed.config)
    assert config.mode == "keys"
    assert config.port == 18511


def test_existing_canvas_url_owns_listener_and_browser_prefix(tmp_path):
    folder = tmp_path / "services/canvas"
    folder.mkdir(parents=True)
    path = folder / "config.json"
    path.write_text(json.dumps({"mode": "keys", "keys": {"file": "keys.json"},
        "url": "http://127.0.0.1:18611/drawing/mcp", "port": 18611}))
    before = path.read_bytes()
    result = setup.plan({}, "C:/release", str(tmp_path), "", ["canvas"],
                        "Robin", "BOX", apply=True)
    assert path.read_bytes() == before
    item = next(s for s in result["services"] if s["name"] == "boring-canvas")
    assert item["url"] == "http://127.0.0.1:18611/drawing/mcp"
    config = ServiceConfig(path)
    store = CanvasStore(folder / "state")
    try:
        app = App(store, config)
        assert app.base == "http://127.0.0.1:18611/drawing"
        assert app.config.port == 18611
    finally:
        store.close()
