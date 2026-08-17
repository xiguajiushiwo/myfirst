from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_camera_and_workbench_use_same_preview_workbench():
    server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

    assert "def workbench()" in server
    assert "def camera_page()" in server
    assert server.count('return _page("camera.html")') >= 2
    assert 'return _page("camera_debug.html")' not in server


def test_realtime_workbench_has_preview_and_capture_together():
    html = (ROOT / "web" / "camera.html").read_text(encoding="utf-8")

    assert "双相机实时画面" in html
    assert "拍照并检测" in html
    assert "capture-and-recognize" in html
    assert "CLIENT_AGENT" in html
    assert "相机调试" not in html


def test_home_does_not_link_to_debug_camera_page():
    html = (ROOT / "web" / "home.html").read_text(encoding="utf-8")

    assert "实时检测工作台" in html
    assert "相机调试" not in html
    assert 'href="/workbench"' in html
