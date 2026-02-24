# Ashlar AO — Agent Orchestrator

A local-first platform for managing AI coding agents from a single command center.

## Quick Start

```bash
./start.sh
```

Then open http://localhost:5000

## Requirements

- Python 3.11+
- tmux (`brew install tmux`)
- Claude Code CLI (optional — runs in demo mode without it)

## Project Structure

```
ashlar_server.py          ← The entire backend (aiohttp + agent management)
ashlar_dashboard.html     ← The entire frontend (served by the backend)
requirements.txt          ← 4 Python dependencies
ashlar.yaml              ← Default configuration
start.sh                 ← Launch script
```

## Development

Read `CLAUDE.md` for full architecture and implementation details.
Read `PHASE1_SPEC.md` for the Phase 1 implementation specification.
Read `REFERENCE_PATTERNS.md` for proven code patterns to build from.
Read `ARCHITECTURE.md` for the complete product vision and design.
