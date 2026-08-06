---
schema_version: "1.0"
document_id: VEP-ARCH-TECH
title: "技術基準"
document_type: architecture
status: draft
canonical: true
canonical_for: technology-baseline
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-ARCH-PIPELINE", "VEP-ARCH-ARTIFACTS", "VEP-ARCH-TRUST", "VEP-AGENT-GOV", "VEP-OPS-CI", "VEP-ADR-INDEX"]
---
# 技術基準

## 1. State

`required`、`preferred`、`optional`、`deferred`、`rejected`を使用します。具体versionは
`pyproject.toml`、`uv.lock`、workflow pinを正本とします。

## 2. Foundation implemented

| 項目 | 技術 | 状態 |
|---|---|---|
| Runtime baseline | Python 3.12 | required |
| Dependency manager | uv | required／configured |
| Document／config parsing | PyYAML、tomllib | required／configured |
| Tests | pytest | required／configured |
| Lint／format | Ruff | required／configured |
| CI | GitHub Actions | required／configured |
| Dependency updates | Dependabot uv／github-actions | configured |
| Agent policy | `.agents/`＋thin adapters | required／configured |
| Skill registry | TOML＋SKILL.md＋eval TOML | required／configured |

## 3. Domain implementation baseline

| 項目 | 技術 | 状態 |
|---|---|---|
| CLI | Typer | preferred |
| Domain validation | Pydantic v2 | required |
| Machine artifacts | JSON／JSONL | required |
| Local search | SQLite FTS5 | preferred |
| Hash | SHA-256 | required |
| direct HTTP | httpx | preferred |
| generic/Panopto backend | yt-dlp adapter | preferred |
| Box API | official API adapter | optional |
| media | FFmpeg／ffprobe | required |
| scene candidate | PySceneDetect AdaptiveDetector | preferred |
| local ASR | faster-whisper | preferred |
| cloud transcription | explicit permission only | optional |
| canonical draft | Markdown | required |
| DOCX／Typst／Pandoc／HTML | export adapters | optional |

## 4. Deferred／rejected

- 全frame OCR: deferred
- provider-fixed AI orchestration: deferred
- Airflow／Prefect／Dagster: deferred
- Docker-only runtime: rejected
- single giant Skill: rejected
- author and auditor same context: rejected
- actual lecture media in CI: rejected

required／preferred技術の変更またはdeferred技術の主要導入には原則ADRを作成します。
