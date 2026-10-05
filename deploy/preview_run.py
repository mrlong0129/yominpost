"""UI 冒烟预览实例:临时数据 / 关鉴权 / 模板驱动。不碰生产数据。
用绝对路径 + 显式 chdir,避免 .nosync 目录下预览器 getcwd 权限问题。
数据目录 /tmp/yp_preview/* 由外部预先灌好(可 copy 生产快照)。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

os.environ.update({
    "YOMINPOST_DATA_DIR": "/tmp/yp_preview/data",
    "YOMINPOST_DB": "/tmp/yp_preview/data/db.sqlite",
    "YOMINPOST_ARTIFACT_DIR": "/tmp/yp_preview/art",
    "YOMINPOST_PROVIDER": "template",
    "YOMINPOST_ENABLE_SCHEDULER": "0",
    "YOMINPOST_DISABLE_CODEX_IMAGE": "1",
})
os.environ.pop("YOMINPOST_ACCESS_KEY", None)   # 预览关鉴权

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("yominpost.web.app:app", host="127.0.0.1",
                port=int(os.environ.get("PORT", "8401")))
