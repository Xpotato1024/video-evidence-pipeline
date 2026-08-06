---
schema_version: "1.0"
document_id: VEP-ARCH-TECH
title: "技術基準"
document_type: architecture
status: draft
canonical: true
canonical_for: technology-baseline
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:07:00+09:00
authors:
  - Xpotato1024
  - OpenAI ChatGPT
owners:
  - Xpotato1024
reviewers: []
source_issue: 1
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-ARCH-PIPELINE", "VEP-ARCH-ARTIFACTS", "VEP-ARCH-TRUST", "VEP-ADR-INDEX"]
---
# 技術基準

## 1. 状態の意味

- `required`: MVPの基盤として採用
- `preferred`: 標準実装候補
- `optional`: job設定により利用
- `deferred`: 初期実装では導入しない
- `rejected`: 現時点では採用しない

この文書は技術採用状態の正本です。具体的なversionは将来の`pyproject.toml`、`uv.lock`、tool manifestを正本とします。

## 2. Core

| 項目 | 技術 | 状態 | 理由 |
|---|---|---|---|
| Runtime | Python 3.12 | required | 型、CLI、media/AI library ecosystem |
| Dependency manager | uv | required | lock、環境、script実行の統合 |
| CLI | Typer | preferred | subcommandと型付きargument |
| Domain validation | Pydantic v2 | required | Schema生成とruntime validation |
| Human config | YAML | preferred | job記述の可読性 |
| Machine artifacts | JSON / JSONL | required | 構造化・streaming・検証 |
| Local search | SQLite FTS5 | preferred | 単一ユーザーで十分、再生成可能 |
| Hash | SHA-256 | required | artifact identityとapproval binding |

## 3. Acquisition

| 技術 | 状態 | 用途 |
|---|---|---|
| pathlib / shutil | required | local source |
| httpx | preferred | direct HTTP |
| yt-dlp | preferred | Panopto／generic source backend |
| Box API | optional | file ID／versionに基づく取得 |
| Browser cookie export file | rejected as default | 漏えい範囲が大きい |
| `cookies-from-browser`相当 | optional | 認証済み取得の局所利用 |

source固有処理はadapterへ隔離し、yt-dlpをdomain APIとして直接露出しません。

## 4. Media

| 技術 | 状態 | 用途 |
|---|---|---|
| FFmpeg | required | audio/video normalization |
| ffprobe | required | stream／duration／codec manifest |
| PySceneDetect AdaptiveDetector | preferred | scene candidate |
| perceptual hash | preferred | duplicate frame削減 |
| contact sheet | required capability | AI／人間の一覧確認 |
| 全frame OCR | deferred | コストと誤認識が大きい |

標準音声はmono 16 kHz FLACを候補とします。原本は再encodeせず保持します。

## 5. Transcription

| 技術 | 状態 | 用途 |
|---|---|---|
| official captions | preferred first source | 固有名詞、講義字幕 |
| faster-whisper | preferred | local ASR |
| word timestamp | required capability | evidence alignment |
| VAD | preferred | silence処理 |
| cloud transcription | optional | 明示許可時の照合 |
| diarization | deferred | 話者区別が必要なjobで追加 |

## 6. AI integration

| 項目 | 状態 |
|---|---|
| Repository-local Skills | preferred |
| Structured outputs / JSON Schema | required capability |
| ChatGPT/Codex interactive execution | supported |
| Provider-fixed API orchestration | deferred |
| Single giant Skill | rejected |
| Author and auditor same context | rejected |

## 7. Export

| 技術 | 状態 | 用途 |
|---|---|---|
| Markdown | required canonical draft | diffと監査 |
| python-docx | optional | 指定Word様式 |
| Typst | optional | 高品質PDF |
| Pandoc | optional | 汎用変換 |
| HTML | optional | インタラクティブ教材 |

## 8. Quality

| 技術 | 状態 |
|---|---|
| pytest | required |
| Ruff | required |
| mypy | preferred |
| GitHub Actions | required |
| synthetic E2E fixture | required |
| actual lecture media in CI | rejected |
| pre-commit | preferred |
| secret/media validator | required capability |

## 9. Orchestration

初期はPython CLIと明示的なstate machineを採用します。

| 技術 | 状態 |
|---|---|
| Python state machine / service layer | preferred |
| Airflow | deferred |
| Prefect | deferred |
| Dagster | deferred |
| Docker-only runtime | rejected |
| Optional reproducibility container | deferred |

単一ユーザーのローカル処理でworkflow engineを先行導入すると、ブラウザ認証、GPU、WSL、FFmpegの運用が複雑化するためです。

## 10. 変更方法

required／preferred技術の変更、またはdeferred技術の主要導入には原則ADRを作成します。
