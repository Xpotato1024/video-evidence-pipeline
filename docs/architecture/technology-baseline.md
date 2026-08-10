---
schema_version: "1.1"
document_id: VEP-ARCH-TECH
title: "技術基準"
document_type: architecture
status: draft
canonical: true
canonical_for: technology-baseline
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-10T18:18:37+09:00
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
| Python package | `video_evidence_pipeline`（`src/` layout） | required／implemented |
| CLI executable | `vep` | required／implemented |
| JSON Schema | Pydantic modelから決定的生成 | required／implemented |

## 3. Domain implementation baseline

| 項目 | 技術 | 状態 |
|---|---|---|
| CLI | Typer | required／implemented through `EVIDENCE_READY` |
| Domain validation | Pydantic v2、unknown field拒否 | required／implemented |
| Machine artifacts | UTF-8 JSON／append-only JSONL | required／implemented |
| Local search | SQLite FTS5 `trigram`、private rebuildable cache | preferred／implemented |
| Hash | streaming SHA-256 | required／implemented |
| local source | `local_file` adapter | required／implemented |
| direct HTTP | httpx 0.28.1 + httpcore 1.0.9 | required／implemented |
| generic/Panopto backend | pinned yt-dlp adapter | preferred／implemented |
| Panopto API | official REST v1 contract | preferred／implemented |
| Box API | official API adapter | optional／implemented |
| media | pinned FFmpeg／ffprobe toolchain manifest、generic decoder-limit capability／executable SHA検証 | required／implemented |
| scene candidate | FFmpeg `select` scene score + periodic + pure-Python pHash | required／implemented |
| caption transcription | normalized subtitle、strict segment／lineage model | required／implemented |
| local ASR | lazy faster-whisper adapter、explicit existing model directory、offline only | preferred／implemented adapter、runtime package optional |
| semantic evidence exchange | fixed JSON request／strict provider-neutral JSON response | required／implemented |
| cloud transcription | explicit permission only | optional／not implemented |
| canonical draft | Markdown | required |
| DOCX／Typst／Pandoc／HTML | export adapters | optional |

network acquisition、media normalization、transcription、semantic evidence exchange、private search cacheは実装済みです。
faster-whisperは新しいlock dependencyにせず、ASRが必要なjobだけでlazy importします。model size名を受け付けず、
必要fileが揃った既存local directoryと`local_files_only`を必須にし、package／model不在をstructured errorとして返します。
cloud ASR／vision routeは実装していません。具体dependency versionは
`pyproject.toml`と`uv.lock`を正本とし、yt-dlpのruntime self-updateは許可しません。SSRF防御の接続先IP pinningは
HTTPX/httpcoreのtransport contractへ依存するため、監査済みのexact versionをdirect dependencyとして固定します。
更新は別PRでcompatibility testとsecurity auditを通します。

The media baseline is provisioned by the pinned manifest at
`tools/ffmpeg-toolchain.json` on Windows. The installer verifies the archive
and executable SHA-256 values, and the runtime records and revalidates the
selected executable identity for each normalization run.

## 4. Deferred／rejected

- 全frame OCR: deferred
- provider-fixed AI orchestration: deferred
- Airflow／Prefect／Dagster: deferred
- Docker-only runtime: rejected
- single giant Skill: rejected
- author and auditor same context: rejected
- actual lecture media in CI: rejected
- PySceneDetect dependency: deferred（現MVPはFFmpeg scene filterでprocess／identity境界を統一）

required／preferred技術の変更またはdeferred技術の主要導入には原則ADRを作成します。
