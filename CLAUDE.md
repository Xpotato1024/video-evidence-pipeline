# Claude Code instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

@.agents/policy.md
@.agents/governance.md

対象Issue／PRと文書台帳を先に読み、共通ポリシーを優先してください。認証情報・実メディア・
ユーザー成果物をGitへ追加せず、実行していない検証を成功と報告しないでください。

このファイルは`metadata_mode: registry-only`です。汎用YAML front matterを追加しません。

変更に必要な検証だけを`.agents/policy.md`に従って選びます。同条件の有効な結果は再利用し、環境エラーは失敗箇所から再開します。全件pytestを毎変更の必須条件にしません。
