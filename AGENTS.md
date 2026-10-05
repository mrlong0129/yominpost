# AGENTS.md: YominPost

Instructions for coding agents (Claude Code, Cursor, Codex, etc.) that want to **use** YominPost or **work on** this repo.

## Use it in one step (no install, no config)

```bash
uvx --from git+https://github.com/mrlong0129/yominpost yominpost --open          # web studio on http://127.0.0.1:8300
uvx --from git+https://github.com/mrlong0129/yominpost yominpost run --brand "Acme" \
  --source "<what the brand does / the announcement>" --platform x --platform linkedin --json
```

Requires only [uv](https://docs.astral.sh/uv/). Defaults need zero configuration: no API keys, data in `~/.yominpost/`, offline `template` driver. Optional: `YOMINPOST_PROVIDER=claude_code|codex|anthropic` for real-model copy, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` for images. See `.env.example`.

Register the MCP server:

```bash
claude mcp add yominpost -- uvx --from git+https://github.com/mrlong0129/yominpost yominpost-mcp
```

Cursor (`~/.cursor/mcp.json` or `.cursor/mcp.json`):

```json
{"mcpServers": {"yominpost": {"command": "uvx", "args": ["--from", "git+https://github.com/mrlong0129/yominpost", "yominpost-mcp"]}}}
```

MCP tools:

| Tool | Purpose |
|---|---|
| `generate_posts(brand, material, platforms, num_topics, trends, language)` | Run the 8-slot pipeline; returns per-platform hook/body/CTA/hashtags, format decision + reasons, QA score/issues, poster/SRT paths |
| `check_post(text, platform, hashtags)` | Pre-publish risk lint (prompt leak, engagement bait, hashtag/link stuffing, length). `ok=false` means do not publish as-is |
| `save_draft(text, title, hook, cta, hashtags, platforms)` | Save a draft in the local workspace targeting connected channels |
| `list_posts(status, limit)` / `list_platforms()` | Inspect posts and channels |

Install the skill (works even without the package):

```bash
mkdir -p ~/.claude/skills/yominpost && curl -fsSL https://raw.githubusercontent.com/mrlong0129/yominpost/main/skill/SKILL.md -o ~/.claude/skills/yominpost/SKILL.md
```

## Rules when acting for a user

- **Never publish on your own.** There is no publish tool on purpose. Save drafts and tell the user to review and publish at http://127.0.0.1:8300.
- Always run `check_post` on anything you propose, and fix `block` issues before showing it.
- Don't paste generation instructions into the post text ("Write a post about…", "Here is a post"). That is the `prompt_leak` signal platforms punish.
- Generated `poster_svg` / `storyboard_srt` are local file paths, not public URLs.

## Work on this repo

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q          # offline, fast, no keys; must pass before committing
.venv/bin/yominpost serve    # run the app (or ./run.sh)
```

- Python package in `yominpost/`: `stages/` (8 capability slots), `orchestrator.py`, `services.py` (compose / posts / publish / sync), `connectors/` + `oauth_specs.py` + `oauth_flow.py`, `platform_risk.py`, `account_intel.py` / `platform_health.py` / `operations.py`, `scheduler.py`, `db.py` (SQLite, column + JSON payload), `web/app.py` + `web/static/` (no build step), `mcp_server.py`, `cli.py`.
- Tests isolate data via `tests/conftest.py` (temp dirs, `template` driver, scheduler off). Keep it that way: tests must never hit the network or real accounts.
- New platform: add a `PlatformMeta` in `models.py`, an OAuth spec in `oauth_specs.py` or a connector in `connectors/real.py`, register in `connectors/registry.py`, and add an offline test with a fake HTTP layer.
- New driver: implement `providers/base.py:Provider`, wire it in `config.make_provider`; it must fall back to `template` on any failure.
- Never commit real tokens, account IDs, customer data or absolute local paths. Use placeholders (`Acme`, `example.com`).
