---
name: yominpost
description: Write, adapt and safety-check social media posts for X, LinkedIn, Instagram, TikTok, YouTube, Xiaohongshu, Telegram, Mastodon, Bluesky and more. Use when the user wants posts generated from material (an announcement, article, product page or notes), wants one idea adapted per platform, asks whether a post is safe to publish, or wants drafts queued for review. Uses the YominPost MCP tools or CLI when available; otherwise follows the same playbook by hand. Never publishes without the user.
---

# Social posts that fit each platform and don't get the account flagged

Goal: turn the user's material into one native post per platform, check each for platform-risk signals, and hand drafts to the user for approval. Publishing is always the user's call.

## 0. If YominPost is available, use it

- MCP: `generate_posts(brand, material, platforms, num_topics)` → `check_post(text, platform)` on each candidate → `save_draft(...)` for the ones the user likes → tell them to review at http://127.0.0.1:8300.
- CLI: `yominpost run --brand "<brand>" --source "<material>" --platform x --platform linkedin --json`.
- Not installed but `uv` is available? Zero install/config: `uvx --from git+https://github.com/mrlong0129/yominpost yominpost run ... --json`. Start the studio with `uvx --from git+https://github.com/mrlong0129/yominpost yominpost --open`.
- The default `template` driver is an offline scaffold. Polish its output yourself, or suggest `YOMINPOST_PROVIDER=claude_code` for model-written copy.

Everything below is the same playbook done by hand.

## 1. Pick the format per platform (decide before writing)

| Platform | Default format | Notes |
|---|---|---|
| X | text ≤ 280 chars; thread for stories / lists | Hook in the first line; at most 1 link |
| LinkedIn | text, 600–1,300 chars | Short paragraphs, a concrete takeaway, 3–5 hashtags max |
| Instagram / Xiaohongshu | image post or carousel | Caption ≤ 2,200 (Xiaohongshu ≤ 1,000); visual carries the message |
| TikTok / YouTube Shorts | short video | Write a storyboard: hook line (≤ 3 s), 3–6 scenes, on-screen text, caption |
| Telegram / Discord / Mastodon / Bluesky | text | Community tone; Bluesky ≤ 300 chars, Mastodon ≤ 500 |

Explain your choice in one line ("story angle → thread").

## 2. Write each version natively

Hook, body, CTA, hashtags. Rewrite for the platform, don't truncate. Keep the user's facts; never invent numbers, customers or quotes. Match the material's language unless asked otherwise.

## 3. Self-critique once

Score clarity, hook strength, platform fit and compliance. If anything is weak, rewrite once and keep the better version.

## 4. Pre-publish risk check (do this for every post)

Block and fix if you see:
- **Prompt leak**: instructions or scaffolding left in the text ("Write a post…", "Here is a post", "As an AI", "[Hook]", "请生成", "以下是").
- **Engagement bait**: "Comment X and I'll DM you", "tag 3 friends", "like & share to win", "评论领取", "转发抽奖". Meta platforms penalise this hard.
- **Over length** for the platform.

Warn if you see: more than ~5 hashtags (Instagram allows more, but stuffing is a spam signal), more than 1 link, the same link or near-identical text posted repeatedly, or a brand-new account posting several times a day.

## 5. Hand off, don't publish

Show the drafts with the format reason and risk-check result, then save them as drafts (YominPost `save_draft`) or give them to the user to post. Never claim something was published unless the user did it or explicitly asked you to and you did it with a real publishing tool.
