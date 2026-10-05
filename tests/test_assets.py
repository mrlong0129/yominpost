"""素材库分类(文件夹式:published / uploaded / external)与整理接口测试。"""
import io
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")

_PNG = bytes.fromhex(  # 1x1 透明 PNG
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def _ws_id(client):
    return client.get("/api/workspaces").json()[0]["id"]


def test_source_derivation_unit():
    from yominpost import services
    from yominpost.models import Asset
    up = Asset(id="a", workspace_id="w", path="/x/up_abc.png")
    pub = Asset(id="b", workspace_id="w", path="/x/ws_c.png", meta={"post_id": "p"})
    ext = Asset(id="c", workspace_id="w", meta={"source": "external",
                                                "site": "helium10", "channel": "blog"})
    assert services.asset_source(up) == "uploaded"
    assert services.asset_source(pub) == "published"
    assert services.asset_source(ext) == "external"
    assert services.asset_folder(ext) == "helium10/blog"
    assert services.asset_folder(pub) == ""


def test_external_ingest_folders_and_filter(client):
    ws = _ws_id(client)
    payload = {"items": [
        {"title": "How to optimize an Amazon listing", "url": "https://www.helium10.com/blog/x1",
         "kind": "article", "site": "helium10", "channel": "blog",
         "summary": "他们主打关键词密度打分。", "tags": ["listing", "seo"]},
        {"title": "Perci AI listing demo", "url": "https://www.perci.ai/watch/y2",
         "kind": "video", "site": "perci", "channel": "youtube",
         "summary": "一键生成五点描述的演示。", "tags": ["ai", "demo"]},
    ]}
    r = client.post(f"/api/workspaces/{ws}/assets/external", json=payload).json()
    assert r["added"] == 2 and r["skipped"] == 0

    # 去重:同 url 再灌一次,只 skip 不新增
    r2 = client.post(f"/api/workspaces/{ws}/assets/external", json=payload).json()
    assert r2["added"] == 0 and r2["skipped"] == 2

    # 分类计数
    folders = client.get(f"/api/workspaces/{ws}/assets/folders").json()
    assert folders["sources"]["external"] == 2
    assert folders["external_folders"]["helium10/blog"] == 1
    assert folders["external_folders"]["perci/youtube"] == 1

    # ?source 过滤 + 派生字段回传
    ext = client.get(f"/api/workspaces/{ws}/assets", params={"source": "external"}).json()
    assert len(ext) == 2
    assert {a["folder"] for a in ext} == {"helium10/blog", "perci/youtube"}
    assert all(a["source"] == "external" for a in ext)

    # ?folder 前缀过滤
    h = client.get(f"/api/workspaces/{ws}/assets", params={"folder": "helium10"}).json()
    assert len(h) == 1 and h[0]["meta"]["site"] == "helium10"


def test_search_and_recommend(client):
    ws = _ws_id(client)
    client.post(f"/api/workspaces/{ws}/assets/external", json={"items": [
        {"title": "Amazon keyword research guide", "url": "https://ex.com/kw",
         "kind": "article", "site": "helium10", "channel": "blog",
         "summary": "关键词研究方法", "tags": ["keyword", "seo"]},
        {"title": "PPC advertising video", "url": "https://ex.com/ppc",
         "kind": "video", "site": "junglescout", "channel": "youtube",
         "summary": "ad budget tips", "tags": ["ppc", "ads"]},
    ]})
    # ?q 全文搜索
    hits = client.get(f"/api/workspaces/{ws}/assets", params={"q": "keyword"}).json()
    assert any("keyword" in (a["title"] + str(a["meta"])).lower() for a in hits)
    assert all("keyword" in (a["title"] + str(a["meta"]) + a["url"]).lower() for a in hits)
    # ?kind + ?channel 过滤
    vids = client.get(f"/api/workspaces/{ws}/assets", params={"kind": "video", "channel": "youtube"}).json()
    assert vids and all(a["kind"] == "video" for a in vids)
    # 推荐:按文本给最相关素材
    rec = client.post(f"/api/workspaces/{ws}/assets/recommend",
                      json={"text": "help me write amazon keyword research seo", "limit": 3}).json()
    assert rec["items"] and "keyword" in rec["items"][0]["title"].lower()


def test_upload_tagged_uploaded(client):
    ws = _ws_id(client)
    files = {"file": ("checklist.png", io.BytesIO(_PNG), "image/png")}
    up = client.post(f"/api/workspaces/{ws}/assets/upload", files=files).json()
    assert up["id"]
    uploaded = client.get(f"/api/workspaces/{ws}/assets", params={"source": "uploaded"}).json()
    assert any(a["id"] == up["id"] and a["source"] == "uploaded" for a in uploaded)


def test_move_and_delete(client):
    ws = _ws_id(client)
    r = client.post(f"/api/workspaces/{ws}/assets/external", json={"items": [
        {"title": "StoreClaw feature page", "url": "https://storeclaw.ai/f/z3",
         "kind": "link", "site": "storeclaw", "channel": "site"}]}).json()
    aid = r["ids"][0]
    # 整理:移动到新文件夹
    moved = client.patch(f"/api/workspaces/{ws}/assets/{aid}",
                         json={"folder": "storeclaw/pricing"}).json()
    assert moved["folder"] == "storeclaw/pricing"
    # 删除(仅该条消失,不假设其它测试留下的 external 素材数量)
    d = client.delete(f"/api/workspaces/{ws}/assets/{aid}").json()
    assert d["deleted"] is True
    ext = client.get(f"/api/workspaces/{ws}/assets", params={"source": "external"}).json()
    assert aid not in {a["id"] for a in ext}
