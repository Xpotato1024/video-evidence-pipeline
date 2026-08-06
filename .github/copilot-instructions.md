# GitHub Copilot instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

Read `.agents/policy.md`, `.agents/governance.md`, the target Issue/PR, and the document registry before
changing files. Do not commit credentials, signed URLs, real media, transcripts, or user deliverables.
Do not treat draft Skills as executable. Keep authoring and independent audit in separate contexts.

This repository-wide instruction file uses `metadata_mode: registry-only`; do not add generic YAML
front matter. Path-specific `.github/instructions/*.instructions.md` files keep their required
`applyTo` front matter.

Use issue-scoped Draft PRs. Run Ruff, pytest, and `scripts/validate_repository.py` before completion.
Do not claim a check passed unless its command was actually executed.
