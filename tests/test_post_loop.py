"""Post Creating Loop:analyze/research/strategy 任务、语言解析、异步 run、驱动溯源。"""
import time
import warnings

warnings.filterwarnings("ignore")

from yominpost import db, services  # noqa: E402
from yominpost.models import Channel, ChannelStatus, ComposeRequest  # noqa: E402
from yominpost.providers.template_provider import TemplateProvider  # noqa: E402

EN_MATERIAL = ("Acme Listing Optimizer saves Amazon sellers 3 hours per listing. "
               "Writing listings by hand is slow and painful. "
               "It generates optimized titles, bullets and keywords automatically.")


def _ws_with_channel(platform="facebook", language=""):
    ws = db.create_workspace("Loop T", brand_name="Acme")
    ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform=platform,
                 display_name="T", status=ChannelStatus.connected,
                 config=({"language": language} if language else {}))
    db.save_channel(ch)
    return ws, ch


# ---------------------------- 新任务模板回退 ---------------------------- #
def test_template_analyze_research_strategy_shapes():
    t = TemplateProvider()
    a = t.run("analyze", {"material": EN_MATERIAL, "notes": "指令不应出现"})
    assert a["topic_title"] and a["language_hint"] == "en"
    assert "指令不应出现" not in a["topic_title"]      # notes 是指令,不是内容
    r = t.run("research", {"platform": "facebook", "analysis": a})
    assert r["winning_formats"] and r["angles"][0]["angle"]
    s = t.run("strategy", {"platform": "facebook", "analysis": a, "research": r,
                           "available_kinds": ["text"], "target_format": "auto"})
    assert s["format"] == "text"                        # 无 image 能力 → 不产图文
    assert s["angle"] and s["hook_direction"]


def test_template_copy_english():
    t = TemplateProvider()
    c = t.run("copy", {"language": "en", "material": EN_MATERIAL,
                       "topic": {"title": "Listing Optimizer"}, "format": "text"})
    text = c["hook"] + c["body"] + c["cta"]
    import re
    assert not re.search(r"[一-鿿]", text), f"English copy 不应含中文: {text[:80]}"


# ------------------------------ 语言解析 ------------------------------ #
def test_language_resolution_priority():
    ws, ch = _ws_with_channel(language="en")
    req = ComposeRequest(source="x", channel_ids=[ch.id], language="zh")
    assert services._resolve_language(req, ch, {"language_hint": "en"}) == "zh"  # 请求最高
    req2 = ComposeRequest(source="x", channel_ids=[ch.id])                       # auto
    assert services._resolve_language(req2, ch, {"language_hint": "zh"}) == "en"  # 渠道次之
    ch.config = {}
    assert services._resolve_language(req2, ch, {"language_hint": "en"}) == "en"  # 素材推断
    assert services._resolve_language(req2, ch, {}) == "en"                       # 默认(现为 en)


# --------------------------- 同步 compose 全链路 --------------------------- #
def test_compose_loop_produces_language_and_provenance():
    ws, ch = _ws_with_channel(language="en")
    req = ComposeRequest(source=EN_MATERIAL, channel_ids=[ch.id], loop=True)
    result = services.compose(ws, req)
    d = result["drafts"][0]
    assert d["language"] == "en"
    assert d["drivers"].get("copy") == "template"       # 测试环境 = template 驱动
    assert d["fallback"] is True                        # template 非 live → 兜底标记
    assert result["fallback"] is True
    assert result["analysis"]["topic_title"]
    assert any(c["task"] == "research" for c in result["trace"])
    assert any(c["task"] == "strategy" for c in result["trace"])
    # 英文渠道产出不含中文
    import re
    body = d["content"]["hook"] + d["content"]["body"]
    assert not re.search(r"[一-鿿]", body)


def test_compose_no_loop_skips_research():
    ws, ch = _ws_with_channel()
    req = ComposeRequest(source="快速路径素材,不走 research", channel_ids=[ch.id],
                         loop=False, optimize=False)
    result = services.compose(ws, req)
    assert not any(c["task"] == "research" for c in result["trace"])
    assert result["drafts"][0]["plan"]["format"]        # 仍有 plan


# ------------------------------ 异步 run ------------------------------ #
def test_start_compose_async_lifecycle():
    ws, ch = _ws_with_channel(language="en")
    req = ComposeRequest(source=EN_MATERIAL, channel_ids=[ch.id], loop=True)
    run_id = services.start_compose(ws, req)
    assert run_id
    # template 驱动很快;轮询至 done
    for _ in range(60):
        run = db.get_compose_run(run_id)
        if run and run.status == "done":
            break
        time.sleep(0.1)
    run = db.get_compose_run(run_id)
    assert run.status == "done", f"run 未完成: {run.status} {run.error}"
    assert run.drafts and run.drafts[0]["language"] == "en"
    assert run.progress.get("step") == "done"
    assert run.trace


# ------------------------- 渠道语言 PATCH API ------------------------- #
def test_channel_language_patch_api():
    from fastapi.testclient import TestClient
    from yominpost.web.app import app
    ws, ch = _ws_with_channel()
    with TestClient(app) as c:
        r = c.patch(f"/api/channels/{ch.id}", json={"language": "en"})
        assert r.status_code == 200 and r.json()["language"] == "en"
        r2 = c.patch(f"/api/channels/{ch.id}", json={"language": "fr"})
        assert r2.status_code == 400                    # 白名单校验
        r3 = c.get(f"/api/workspaces/{ws.id}/channels")
        got = [x for x in r3.json() if x["id"] == ch.id][0]
        assert got["language"] == "en"


# --------------------------- compose/start API --------------------------- #
def test_compose_start_api_and_poll():
    from fastapi.testclient import TestClient
    from yominpost.web.app import app
    ws, ch = _ws_with_channel(language="en")
    with TestClient(app) as c:
        r = c.post(f"/api/workspaces/{ws.id}/compose/start",
                   json={"source": EN_MATERIAL, "channel_ids": [ch.id], "loop": True})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        for _ in range(60):
            g = c.get(f"/api/compose-runs/{rid}").json()
            if g["status"] == "done":
                break
            time.sleep(0.1)
        assert g["status"] == "done"
        assert g["drafts"][0]["language"] == "en"
        assert g["fallback"] is True                    # 测试环境 template → 显式兜底标记
        assert "visual_url" in g["drafts"][0]           # 恢复路径也补产物 URL


# ------------------------ review 修复的回归测试 ------------------------ #
def test_notes_never_enter_material():
    m = services.parse_material("真实素材内容在此", notes="这是运营者指令")
    assert "这是运营者指令" not in m["combined"]
    m2 = services.parse_material("", notes="只有指令时才兜底")   # 素材全空 → 才退化用 notes
    assert m2["combined"] == "只有指令时才兜底"


def test_compose_request_language_clamped():
    r = ComposeRequest(source="x", language="fr")
    assert r.language == "auto"
    assert ComposeRequest(source="x", language="EN").language == "en"


def test_recover_stuck_compose_runs():
    ws, ch = _ws_with_channel()
    run = services._new_compose_run(ws, ComposeRequest(source="x", channel_ids=[ch.id]),
                                    status="running")
    db.save_compose_run(run)
    n = services.recover_stuck_compose_runs()
    assert n >= 1
    assert db.get_compose_run(run.id).status == "failed"


def test_start_compose_concurrency_cap(monkeypatch):
    ws, ch = _ws_with_channel()
    # 造 3 条 running 占满配额
    for _ in range(services.MAX_RUNNING_COMPOSE):
        db.save_compose_run(services._new_compose_run(
            ws, ComposeRequest(source="x", channel_ids=[ch.id]), status="running"))
    import pytest
    with pytest.raises(ValueError):
        services.start_compose(ws, ComposeRequest(source="x", channel_ids=[ch.id]))
    services.recover_stuck_compose_runs()               # 清场,避免污染其他测试


def test_trace_design_split_attributed_to_template():
    from yominpost.providers.cli_provider import ClaudeCodeProvider
    p = ClaudeCodeProvider(cli_tasks={"copy"})          # qa 不在 cli 任务里 → 设计内走模板
    r = p.run("qa", {"copy": {"hook": "h", "body": "b", "cta": "c"}, "brand": {}, "topic": {}})
    assert r["_meta"]["driver"] == "template"
