---
applyTo: ".agents/**,AGENTS.md,CLAUDE.md,GEMINI.md,.github/copilot-instructions.md"
---
Treat `.agents/policy.md`, `.agents/governance.md`, and `.agents/skill-system.toml` as the common source of
truth. Keep vendor adapters thin. Every active or draft Skill needs a registry entry and an eval file.
Draft Skills must not auto-trigger or perform side effects.
