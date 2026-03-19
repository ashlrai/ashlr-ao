# AO — Agent Orchestrator | For Sale

**Production-ready agent orchestration platform — 143K lines, 2,700+ tests, desktop app, marketing website, PyPI package.**

One developer, many AI coding agents (Claude Code, Codex, Aider, Goose), multiple repos, single command center.

---

## Asking Price: $9,500

Negotiable. Serious inquiries only: **tilly@ashlr.ai**

---

## What You Get

| Asset | Details |
|-------|---------|
| **Source code** | 24 Python modules (~15.5K lines server), 17K JS dashboard, 5.8K CSS |
| **Test suite** | 2,719 tests across 31 test files, 65%+ coverage |
| **Desktop app** | Tauri v2 — 5.9MB .app, 3.1MB .dmg (macOS), ready for Windows/Linux builds |
| **Marketing website** | 10 pages (landing, 6 docs, blog, pricing, docs index) deployed on Vercel |
| **PyPI package** | `pip install ashlr-ao` — installable CLI (`ashlr`) |
| **GitHub repo** | Full git history (30+ dev sessions), 3 CI/CD workflows (tests, deploy, publish) |
| **Domains** | ashlrao.com (Vercel), ashlr.ai |
| **Brand assets** | Logo, OG images, Twitter cards, screenshots, favicon |
| **Documentation** | CLAUDE.md (comprehensive), API reference, getting started guide, deployment docs |
| **Licensing system** | Ed25519 JWT — Community/Pro tiers, offline-first, no phone-home |
| **License** | MIT — buyer can relicense |

## Tech Stack

| Layer | Choice |
|-------|--------|
| Server | Python 3.11+ / aiohttp (async HTTP + WebSocket) |
| Frontend | Vanilla JS (ES2022+) — zero dependencies, no build step |
| Persistence | SQLite via aiosqlite — zero-config |
| Process mgmt | tmux — session isolation, output capture |
| Desktop | Tauri v2 (Rust) — native wrapper, system tray, sidecar server |
| Licensing | PyJWT + Ed25519 — offline signed JWT |
| Intelligence | xAI Grok (optional) — OpenAI-compatible API |
| Voice | Web Speech API — browser-native push-to-talk |
| CI/CD | GitHub Actions — tests (Python 3.11-3.13), PyPI publish, Vercel deploy |

## Key Metrics

- **143,821 lines of code** (Python, JS, CSS, HTML, Rust, TOML)
- **2,719 passing tests** across 31 test files
- **40+ REST API endpoints** with full CRUD
- **WebSocket protocol** for real-time updates
- **24 Python modules** — clean, modular architecture
- **v1.6.1** — mature, versioned, packaged
- **30+ development sessions** — battle-tested through extensive iteration

## Features

### Core Orchestration
- Spawn and manage multiple AI coding agents simultaneously
- Real-time dashboard with live terminal output, ANSI color rendering
- Agent status detection (planning/working/waiting/error/idle)
- Inline agent interaction — approve, reject, custom responses
- Bulk operations — pause/resume/restart/kill multiple agents
- Command bar with natural language routing (LLM-powered)

### Multi-Agent Intelligence
- Fleet analysis — detect conflicts, stuck agents, handoff opportunities
- Auto-handoff — agents spawn successors on completion
- File conflict detection — warns when agents edit the same file
- Cross-agent scratchpad — shared notes with WebSocket broadcast
- Agent health scoring with auto-pause on critical health

### Enterprise Features (Pro Tier)
- Multi-user auth — session-based, bcrypt, admin/member roles
- Workflow engine — DAG-based agent orchestration with depends_on
- Fleet templates — parameterized, one-click deploy
- Auto-pilot — auto-restart on stall, auto-approve patterns
- Cost tracking and budget alerts

### Developer Experience
- 9 built-in agent roles with icons (frontend, backend, devops, tester, etc.)
- 4 backend integrations (Claude Code, Codex, Aider, Goose)
- Plan mode toggle, model selection, tool restriction
- Quick templates (Code Review, Tests, Bug Fix, Feature, Security, Refactor)
- Keyboard-driven — 15+ shortcuts, command palette (Cmd+K)
- Voice input — push-to-talk and click-to-toggle
- Dark + light themes with glassmorphism design

### IDE Features
- PTY terminals — interactive terminal sessions via WebSocket
- File browser — tree view, read, write, create, delete, rename
- Git integration — status, diff, log, branches, stage, commit, discard
- GitHub integration — PRs, issues, repo stats

### Desktop App (Tauri v2)
- Native macOS app (5.9MB) with system tray
- Sidecar Python server management
- Ready for Windows/Linux builds

## Revenue Potential

The open-core model is **fully built and functional**:

1. **Community tier** (free): 5 agents, 1 user, core orchestration
2. **Pro tier** (paid): Up to 100 agents, 50 users, workflows, fleet presets, intelligence, multi-user auth

What a buyer needs to add:
- Stripe integration for payment processing
- License key portal (generate_license.py already handles key creation)
- Marketing push

The licensing system uses Ed25519-signed JWTs — offline-first, no license server needed. Feature gating returns 403 with upgrade prompts already built into the dashboard.

## Architecture Overview

```
ashlr_ao/
├── server.py          (3.9K) — Route handlers, create_app(), main()
├── manager.py         (1.7K) — Agent lifecycle, tmux orchestration
├── database.py        (1.2K) — Async SQLite persistence
├── background.py      (1.1K) — 6 supervised background tasks
├── intelligence.py    (640)  — LLM integration, health scoring
├── models.py          (660)  — All dataclasses
├── analytics.py       (760)  — Fleet analytics, costs, bulk ops
├── system_endpoints.py(790)  — System metrics, health, config
├── workflow_endpoints.py(570)— Workflow CRUD, fleet templates
├── websocket.py       (400)  — WebSocket hub, metrics
├── auth.py            (350)  — Auth middleware, sessions
├── status.py          (370)  — Agent status detection
├── config.py          (340)  — YAML config management
├── pty.py             (360)  — Interactive terminal sessions
├── files.py           (430)  — File browser API
├── git.py             (420)  — Git integration API
├── middleware.py       (230)  — Rate limiting, CORS, security headers
├── extensions.py      (250)  — Extension scanner
├── backends.py        (160)  — Backend configs
├── constants.py       (160)  — Constants, patterns
├── licensing.py       (145)  — License validation
├── roles.py           (80)   — Built-in roles
├── dashboard.html     (1.5K) — HTML shell
├── static/dashboard.js(17K)  — Dashboard application
└── static/dashboard.css(5.8K)— Styles (dark + light)
```

## Why I'm Selling

Builder moving to other projects. The product is fully functional and tested — it just needs someone with the time and interest to take it to market. The AI agent orchestration space is growing, and this is a ready-made product with no technical debt.

## Transfer Process

1. GitHub repo transfer (or add as collaborator)
2. PyPI package ownership transfer
3. Domain DNS transfer (ashlrao.com, ashlr.ai)
4. Vercel project transfer
5. 1-page "getting started as new owner" guide
6. Optional: 1-2 video calls for knowledge transfer (included)

## Quick Start (Try It Yourself)

```bash
pip install ashlr-ao
ashlr
# Dashboard opens at http://127.0.0.1:5111
```

---

**Interested? Email tilly@ashlr.ai**
