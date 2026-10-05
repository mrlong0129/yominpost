"""Plug-and-play layer: CLI entry points, MCP tools, zero-config defaults."""
import asyncio
import json

import pytest

from yominpost import cli, mcp_server


def test_cli_run_json(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--brand", "Acme", "--source", "We help founders ship content faster",
                  "--platform", "x", "--platform", "linkedin", "--topics", "2", "--json", "--no-persist"])
    assert e.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["provider"] == "template"
    assert len(data["packages"]) == 2
    assert all(p["copy"]["hook"] for p in data["packages"])


def test_cli_rejects_unknown_platform(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--platform", "myspace", "--no-persist"])
    assert e.value.code == 2
    assert "myspace" in capsys.readouterr().err


def test_cli_defaults_to_serve():
    args = cli.build_parser().parse_args(["serve"])
    assert args.func is cli._cmd_serve and args.port == 8300 and args.host == "127.0.0.1"


def test_mcp_generate_posts_offline():
    out = mcp_server.generate_posts_impl("Acme", "Acme turns one idea into ten posts.", ["x", "instagram"], 2)
    assert out["ok"] and out["driver"] == "template"
    assert len(out["packages"]) == 2
    pk = out["packages"][0]
    assert pk["hook"] and pk["platform"] in ("x", "instagram") and pk["qa"]["overall"] >= 0


def test_mcp_generate_posts_bad_platform():
    out = mcp_server.generate_posts_impl("Acme", platforms=["myspace"])
    assert out["ok"] is False and "myspace" in out["error"]


def test_mcp_check_post_flags_risks():
    leak = mcp_server.check_post_impl("请生成一条小红书笔记: here is a post about our product", "instagram")
    assert leak["ok"] is False and any(i["code"] == "prompt_leak" for i in leak["issues"])
    long = mcp_server.check_post_impl("a" * 300, "x")
    assert long["ok"] is False and any(i["code"] == "too_long" for i in long["issues"])
    clean = mcp_server.check_post_impl("Shipping v0.1 today. One idea in, ten native posts out.", "x")
    assert clean["ok"] is True and clean["issues"] == []


def test_mcp_save_draft_and_list():
    out = mcp_server.save_draft_impl("Hello from an agent", title="agent draft", platforms=["tiktok", "linkedin"])
    assert out["ok"] and out["status"] == "draft"
    assert "linkedin" in out["unconnected_platforms"]          # not connected in the seeded workspace
    posts = mcp_server.list_posts_impl("draft")["posts"]
    assert any(p["id"] == out["post_id"] for p in posts)


def test_mcp_registers_tools_without_publish():
    async def names():
        tools = await mcp_server.mcp.list_tools()
        return {t.name for t in tools}
    got = asyncio.run(names())
    assert {"generate_posts", "check_post", "list_platforms", "save_draft", "list_posts"} <= got
    assert not any("publish" in n for n in got)


def test_list_platforms_hides_demo():
    ids = {c["id"] for c in mcp_server.list_platforms_impl()["channels"]}
    assert "telegram" in ids and "oauthdemo" not in ids


def test_default_data_dir_is_outside_source_tree(monkeypatch, tmp_path):
    import importlib

    from yominpost import config
    monkeypatch.delenv("YOMINPOST_DATA_DIR", raising=False)
    monkeypatch.delenv("YOMINPOST_ARTIFACT_DIR", raising=False)
    monkeypatch.setenv("YOMINPOST_HOME", str(tmp_path / "home"))
    try:
        importlib.reload(config)
        if not (config.PACKAGE_DIR / "data").is_dir():
            assert config.DATA_DIR == tmp_path / "home" / "data"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_bare_command_routes_to_serve(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_cmd_serve", lambda a: seen.update(port=a.port) or 0)
    with pytest.raises(SystemExit) as e:
        cli.main(["--port", "8399"])
    assert e.value.code == 0 and seen["port"] == 8399


def test_english_material_gives_english_copy():
    out = mcp_server.generate_posts_impl(
        "Acme", "Acme turns one long-form idea into ten platform-native posts. Founders save hours.", ["x"], 1)
    hook = out["packages"][0]["hook"]
    assert hook and not any("\u4e00" <= ch <= "\u9fff" for ch in hook)
