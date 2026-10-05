"""YominPost — 一个覆盖「8 能力槽」的内容生成流水线。

能力槽 (capability slots):
  1. 品牌知识库 / DNA        brand_dna
  2. 选题 / Ideation / 日历   ideation
  3. 趋势 / 洞察输入          trends
  4. 形式选择 Router(规则)   router
  5. 文案生成                copy
  6. 图文 / 视觉             visual
  7. 短视频                 video
  8. 质量评估 / 自我批评      qa
"""

__version__ = "0.1.0"

CAPABILITY_SLOTS = [
    ("brand_dna", "① 品牌知识库 / DNA", "官网项目 + 旧内容整理，抽取品牌调性与消息"),
    ("ideation", "② 选题 / Ideation / 内容日历", "生成选题并排入内容日历"),
    ("trends", "③ 趋势 / 洞察输入", "该写什么：趋势信号与洞察打分"),
    ("router", "④ 形式选择 Router", "规则化决策:文本 / 图文 / 短视频 …"),
    ("copy", "⑤ 文案生成", "按平台生成 hook / 正文 / CTA / 话题"),
    ("visual", "⑥ 图文 / 视觉", "视觉简报 + 可渲染的海报 (SVG)"),
    ("video", "⑦ 短视频", "分镜脚本 + 字幕 (SRT)"),
    ("qa", "⑧ 质量评估 / 自我批评", "打分 + 违禁词/合规检查 + 自动重写"),
]
