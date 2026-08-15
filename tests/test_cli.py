import pytest

from garmin_mcp import cli


def test_bare_invocation_serves_over_stdio(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.update(vars(args)) or 0)
    assert cli.main([]) == 0
    assert seen["transport"] == "stdio"
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8000
    assert seen["path"] == "/mcp"


def test_serve_flags_are_parsed(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.update(vars(args)) or 0)
    cli.main(["serve", "--transport", "streamable-http", "--port", "9001", "--path", "/mcp-abc"])
    assert seen["transport"] == "streamable-http"
    assert seen["port"] == 9001
    assert seen["path"] == "/mcp-abc"


def test_serve_forwards_transport_options(monkeypatch):
    from garmin_mcp import server

    captured = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: captured.update(kwargs))
    server.run(transport="streamable-http", host="0.0.0.0", port=9002, path="/mcp-xyz")
    assert captured == {
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "port": 9002,
        "streamable_http_path": "/mcp-xyz",
    }


def test_stdio_ignores_http_options(monkeypatch):
    from garmin_mcp import server

    captured = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: captured.update(kwargs))
    server.run(transport="stdio", host="0.0.0.0", port=9002)
    assert captured == {"transport": "stdio"}


def test_public_bind_warns(monkeypatch, capsys):
    from garmin_mcp import server

    monkeypatch.setattr(server, "run", lambda **kwargs: None)
    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--transport", "streamable-http", "--host", "0.0.0.0"])
    cli.cmd_serve(args)
    assert "no authentication" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_bind_is_quiet(monkeypatch, capsys, host):
    from garmin_mcp import server

    monkeypatch.setattr(server, "run", lambda **kwargs: None)
    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--transport", "streamable-http", "--host", host])
    cli.cmd_serve(args)
    assert capsys.readouterr().err == ""
