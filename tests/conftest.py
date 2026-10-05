"""测试隔离:把数据 / 产物 / DB 指向临时目录,强制离线 template provider。"""
import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="yominpost_test_"))
os.environ["YOMINPOST_DATA_DIR"] = str(_tmp / "data")
os.environ["YOMINPOST_ARTIFACT_DIR"] = str(_tmp / "art")
os.environ["YOMINPOST_DB"] = str(_tmp / "data" / "test.db")
os.environ["YOMINPOST_PROVIDER"] = "template"
os.environ["YOMINPOST_ENABLE_SCHEDULER"] = "0"   # 测试不跑后台调度
os.environ["YOMINPOST_DISABLE_CODEX_IMAGE"] = "1"  # 测试不真调 codex 生图
# 软风控门(内容/节奏/重复度)默认关:共享渠道跨用例累积会误触节奏上限,污染无关
# 发布测试。风控本身的用例(test_platform_risk)按需 setenv=1 显式开启。
os.environ["YOMINPOST_RISK_GATE"] = "0"
