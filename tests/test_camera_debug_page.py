from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_camera_and_workbench_pages_are_separate():
    server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

    assert 'def workbench()' in server
    assert 'return _page("camera.html")' in server
    assert 'def camera_page()' in server
    assert 'return _page("camera_debug.html")' in server


def test_camera_debug_page_is_fullscreen_debug_only():
    html = (ROOT / "web" / "camera_debug.html").read_text(encoding="utf-8")

    assert "相机调试" in html
    assert "fullscreen('front')" in html
    assert "fullscreen('back')" in html
    assert "requestFullscreen" in html
    assert "/camera/preview?side=" in html
    assert "capture-and-recognize" not in html
    assert "manual-go" not in html


def test_camera_debug_health_poll_does_not_restart_streams():
    html = (ROOT / "web" / "camera_debug.html").read_text(encoding="utf-8")

    assert "const streamStarted" in html
    assert "function ensureStream(side)" in html
    assert "quality=" in html
    assert "max_fps=" in html
    assert "setInterval(loadHealth, 5000)" in html
    assert "if(devices.length){\n      refreshAll();" not in html
