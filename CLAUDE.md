# Claude Code instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

@.agents/policy.md
@.agents/governance.md

対象Issue／PRと文書台帳を先に読み、共通ポリシーを優先してください。認証情報・実メディア・
ユーザー成果物をGitへ追加せず、実行していない検証を成功と報告しないでください。

このファイルは`metadata_mode: registry-only`です。汎用YAML front matterを追加しません。

変更後は`uv run ruff check scripts tests`、`uv run pytest -q`、
`uv run python scripts/validate_repository.py --root .`を実行します。
