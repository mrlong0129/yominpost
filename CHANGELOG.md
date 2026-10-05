# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-06

First public release.

### Added

- Plug-and-play layer: `pyproject.toml` with `yominpost` and `yominpost-mcp` entry points; zero-install `uvx --from git+https://github.com/mrlong0129/yominpost yominpost` (bare command starts the web app, `--open` opens the browser).
- CLI: `yominpost serve | run | mcp`; `run --json` and `--language auto|en|zh`.
- MCP server (stdio) with tools `generate_posts`, `check_post`, `save_draft`, `list_posts`, `list_platforms`. No publish tool by design.
- Agent skill `skill/SKILL.md`, `AGENTS.md`, `llms.txt`, Cursor / Claude Code MCP examples, English README with a Chinese section, MIT `LICENSE`.
- The 8-slot pipeline now follows the material's language (English material gives English copy in the offline driver).
- Tests for the CLI, MCP tools and zero-config defaults.

### Changed

- Zero-config data location: data and artifacts default to `~/.yominpost/` (override with `YOMINPOST_HOME`, `YOMINPOST_DATA_DIR`, `YOMINPOST_ARTIFACT_DIR`). Existing installs that already have `yominpost/data/` keep using it.
- Daily channel sync uses `YOMINPOST_TZ` or the system time zone instead of a hard-coded zone.
- Deploy scripts no longer hard-code a user, path or domain (`YOMINPOST_PROJECT_DIR`, `YOMINPOST_PUBLIC_URL`).

### Removed

- The private Google Ads workbench / Ads Agent (it depended on a private local harness and was not usable outside the author's setup), its scheduler hooks, UI, API routes and docs. Meta / X ad-account linking on channels is kept.
- Internal planning and review notes from `docs/`.

### Existing capabilities (pre-release, now documented)

- 8-slot content engine, Post Creating Loop with infinite canvas, swappable drivers with fallback chain (template, claude_code, codex, agy, anthropic).
- Real OAuth2 + PKCE / token / webhook connectors for X, LinkedIn, Reddit, Facebook Page, Instagram, YouTube Shorts, Discord, Mastodon, Telegram, Bluesky, Webhook; simulated TikTok, Xiaohongshu, Threads, Pinterest.
- Pre-publish platform risk gate, channel health, calendar, scheduler, asset library, topics, operator auth, encrypted tokens, backups.

[0.1.0]: https://github.com/mrlong0129/yominpost/releases/tag/v0.1.0
