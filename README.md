# 🌊 Pelagic Fleet — Standalone Agent Scaffold

> The core base that **every** agent in the SuperInstance ecosystem inherits from.

## What This Is

This is the foundational CLI agent scaffold for the **Pelagic AI Fleet**. It provides a production-ready base class, onboarding protocol, secret management client, workshop manager, and beautiful terminal UI — all using **only stdlib + PyYAML**.

## Quick Start

```bash
# Onboard a new agent
python -m standalone_agent_scaffold onboard

# Check agent status
python -m standalone_agent_scaffold status

# Run the agent (hot mode)
python -m standalone_agent_scaffold run --mode hot

# Manage the local workshop
python -m standalone_agent_scaffold workshop init
python -m standalone_agent_scaffold workshop history

# Link to a Keeper Agent
python -m standalone_agent_scaffold link-keeper --keeper-url http://localhost:8443

# Review audit trail
python -m standalone_agent_scaffold audit --limit 50
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     CLI (cli.py)                            │
│   argparse subcommands · ANSI TUI · spinners · prompts      │
└──────────┬──────────┬──────────────┬───────────┬────────────┘
           │          │              │           │
     ┌─────▼─────┐ ┌──▼────────┐ ┌──▼──────┐ ┌─▼──────────┐
     │  agent.py │ │ onboard.py│ │ keeper_ │ │ workshop.py│
     │           │ │           │ │ client  │ │            │
     │ Standalone│ │ Onboard   │ │ _client │ │ Workshop   │
     │ Agent     │ │ Protocol  │ │ .py     │ │ Manager    │
     │           │ │           │ │         │ │            │
     │ · State   │ │ · 7 steps │ │ · Store │ │ · Recipes  │
     │   Machine │ │ · Idempo- │ │   Secr. │ │ · Snapshots│
     │ · Config  │ │   tent    │ │ · Proxy │ │ · History  │
     │ · Heart-  │ │ · Secret  │ │   APIs  │ │ · Narrative│
     │   beat    │ │   Scrub   │ │ · Audit │ │ · Rewind   │
     │ · Logging │ │ · Persist │ │   Trail │ │ · Build    │
     └───────────┘ └───────────┘ └─────────┘ └────────────┘
           │              │              │           │
           └──────────────┴──────────────┴───────────┘
                          │
              ┌───────────▼────────────┐
              │   ~/.superinstance/    │
              │   ├── agent.yaml       │
              │   ├── onboard_state.json│
              │   ├── workshop/        │
              │   │   ├── recipes/     │
              │   │   ├── interpreters/│
              │   │   ├── scripts/     │
              │   │   ├── bootcamp/    │
              │   │   └── dojo/        │
              │   └── logs/            │
              └────────────────────────┘
                          │
              ┌───────────▼────────────┐
              │   Keeper Agent (ext.)   │
              │   ┌─────────────────┐  │
              │   │ Secret Vault    │  │
              │   │ API Proxy       │  │
              │   │ Git Proxy       │  │
              │   │ Audit Log       │  │
              │   └─────────────────┘  │
              └────────────────────────┘
```

## Key Principles

| Principle | Implementation |
|-----------|---------------|
| **No secrets on disk** | All secrets go to Keeper; agents only hold references |
| **Idempotent onboarding** | Every step can be re-run safely |
| **Secret scrubbing** | All outbound data scanned for accidental leakage |
| **Stdlib-only** | Only external dep is `pyyaml` |
| **Production quality** | Full type hints, docstrings, error handling |

## State Machine

```
  ┌──────┐  onboard   ┌───────────┐  complete  ┌────────┐
  │ BOOT │ ────────── │ ONBOARDING│ ────────── │ ACTIVE │
  └──┬───┘            └─────┬─────┘            └───┬────┘
     │                      │                      │
     │ archive              │ archive              │ pause
     ▼                      ▼                      ▼
  ┌──────────┐         ┌──────────┐          ┌────────┐
  │ ARCHIVED │         │ ARCHIVED │          │ PAUSED │
  └──────────┘         └──────────┘          └───┬────┘
                                                   │ resume
                                                   ▼
                                              ┌────────┐
                                              │ ACTIVE │
                                              └────────┘
```

## License

MIT
