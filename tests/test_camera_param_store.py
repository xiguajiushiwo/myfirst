import json

from app.cameras import param_store


def test_camera_orientation_is_persisted(tmp_path, monkeypatch):
    path = tmp_path / "camera_params.json"
    monkeypatch.setattr(param_store, "_PATH", str(path))

    param_store.save_role("back", exp_us=12000, gain_db=0, orient="rot180")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["back"] == {"exp_us": 12000.0, "gain_db": 0.0, "orient": "rot180"}
    assert param_store.load_role("back")["orient"] == "rot180"
