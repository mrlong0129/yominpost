"""生图:能力优先级、去水印、codex 简报构造。"""
import os
import warnings

warnings.filterwarnings("ignore")

from yominpost import capabilities as caps, imaging_codex  # noqa: E402


def test_active_image_driver_preference(monkeypatch):
    monkeypatch.delenv("YOMINPOST_DISABLE_CODEX_IMAGE", raising=False)
    monkeypatch.setattr(caps.shutil, "which", lambda n: "/usr/bin/" + n)  # 全部 CLI 视为存在
    # 无 OPENAI key → codex_image 胜出(openai 不可用)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(caps.db, "get_setting", lambda *a, **k: None)
    assert caps.active_image_driver() == "codex_image"
    # 配了 OPENAI key → openai_image 最优先
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert caps.active_image_driver() == "openai_image"
    # 关掉 codex 生图 + 无 openai → 落到内置 svg
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("YOMINPOST_DISABLE_CODEX_IMAGE", "1")
    assert caps.active_image_driver() == "svg"


def test_openai_image_gated_and_prompt(monkeypatch):
    from yominpost import imaging_openai
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("YOMINPOST_IMAGE_API_KEY", raising=False)
    monkeypatch.setattr(imaging_openai, "_setting", lambda k: "")
    # 无 key → render 直接返回 None(不发请求)
    assert imaging_openai.render(subject="x", headline="h", key_points=[], palette=[],
                                 platform="instagram", style="", brand_name="B",
                                 stem="t") is None
    # 画幅映射
    assert imaging_openai.PLATFORM_IMG_SIZE["instagram"] == "1024x1024"  # IG 合法 1:1
    assert imaging_openai.PLATFORM_IMG_SIZE["facebook"] == "1536x1024"
    # prompt 含主题/无水印约束
    p = imaging_openai.build_prompt(subject="Listing optimizer", headline="Fix your listing",
                                    key_points=["a"], palette=["#0B3D2E"], platform="instagram",
                                    style="clean", brand_name="Acme")
    assert "Listing optimizer" in p and "watermark" in p.lower()
    assert "Acme" not in p            # 不再把品牌名渲染进图(避免水印/错误品牌)


def test_openai_image_base_url_configurable(monkeypatch):
    from yominpost import imaging_openai
    monkeypatch.setattr(imaging_openai, "_setting", lambda k: "")
    monkeypatch.delenv("YOMINPOST_IMAGE_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert imaging_openai.base_url() == "https://api.openai.com/v1"
    monkeypatch.setenv("YOMINPOST_IMAGE_BASE_URL", "https://aihubmix.com/v1/")
    assert imaging_openai.base_url() == "https://aihubmix.com/v1"  # 去尾斜杠
    # 专用 IMAGE key 优先于 OPENAI_API_KEY
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("YOMINPOST_IMAGE_API_KEY", "sk-aihubmix")
    assert imaging_openai.api_key() == "sk-aihubmix"


def test_codex_poster_capability_present():
    ids = [c["id"] for c in caps.DEFAULT_CAPABILITIES]
    assert "codex_poster" in ids
    cap = next(c for c in caps.DEFAULT_CAPABILITIES if c["id"] == "codex_poster")
    assert cap["kind"] == "image" and cap["driver"] == "codex_image"


def test_brief_has_no_watermark_and_hard_rules():
    brief = imaging_codex.build_brief(
        headline="Test headline", platform="instagram",
        palette=["#0B3D2E", "#1DB954"], key_points=["a", "b"],
        brand_name="Acme", style="modern", language="en", out_path="/tmp/x.png")
    assert "NO watermark" in brief
    assert "1080x1350" in brief          # IG 尺寸
    assert "/tmp/x.png" in brief
    assert "network" in brief.lower()    # 禁联网


def test_svg_fallback_has_no_watermark():
    from yominpost.stages import visual
    svg = visual._render_svg("hi", "Acme", ["#000", "#fff", "#111", "#222"], "t")
    content = open(svg, encoding="utf-8").read()
    os.remove(svg)
    assert "自动生成海报" not in content and "yominpost ·" not in content


def test_jpg_poster_has_no_watermark(tmp_path, monkeypatch):
    from yominpost import imaging
    monkeypatch.setattr(imaging, "ARTIFACT_DIR", str(tmp_path))
    p = imaging.render_jpg_poster("hi", "Acme", ["#0B3D2E", "#1DB954", "#fff", "#FACC15"], "t")
    assert os.path.exists(p)
    # 仅验证渲染成功且无异常;水印文字已从源码移除
    from PIL import Image
    assert Image.open(p).size == (1080, 1080)
