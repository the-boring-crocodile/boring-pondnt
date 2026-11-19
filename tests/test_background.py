"""The windowless launcher: output to the log, only the core, its own arguments."""
import pytest

from boring_pondnt import background


def test_output_goes_to_the_log_and_the_arguments_through(tmp_path, monkeypatch):
    seen = {}

    def fake_run(name, run_name, alter_sys):
        import sys
        seen["argv"] = list(sys.argv)
        print("started")
    monkeypatch.setattr(background.runpy, "run_module", fake_run)
    monkeypatch.setattr(background.sys, "stdout", None)
    monkeypatch.setattr(background.sys, "stderr", None)
    log = tmp_path / "core.log"
    background.main(["--log", str(log), "boring_pondnt.server", "--config", "C:/x/config.json"])
    assert seen["argv"] == ["boring_pondnt.server", "--config", "C:/x/config.json"]
    background.sys.stdout.flush()
    assert log.read_text(encoding="utf-8") == "started\n"


def test_canvas_arguments_pass_through_without_listener_overrides(tmp_path, monkeypatch):
    from boring_pondnt import setup_services as setup
    seen = []
    monkeypatch.setattr(background.runpy, "run_module",
                        lambda *args, **kwargs: seen.append(list(background.sys.argv)))
    monkeypatch.setattr(background.sys, "stdout", None)
    monkeypatch.setattr(background.sys, "stderr", None)
    monkeypatch.setattr(background.sys, "argv", [])
    action = setup.arguments(setup.SERVICES["canvas"], str(tmp_path), "", str(tmp_path))
    background.main(["--log", str(tmp_path / "service.log"), "boring_canvas.server", *action])
    background.sys.stdout.close()
    assert seen == [["boring_canvas.server", *action]]
    assert not {"--host", "--port", "--public-base-url"} & set(action)


def test_the_d365_server_may_start_this_way_too():
    assert "boring_pondnt.d365_server" in background.ALLOWED


@pytest.mark.parametrize("argv", [[], ["--log", "x"], ["--log", "x", "os"], ["x", "y", "boring_pondnt.server"]])
def test_only_the_core_and_only_with_a_log(argv):
    with pytest.raises(SystemExit):
        background.main(argv)
