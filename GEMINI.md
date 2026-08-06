# Gemini CLI instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

作業開始前に`.agents/policy.md`、`.agents/governance.md`、対象Issue／PR、
`docs/registry/documents.yaml`を読んでください。本ファイルは入口であり、共通ポリシーを
複製・上書きしません。

このファイルは`metadata_mode: registry-only`です。汎用YAML front matterを追加しません。

認証情報・実メディア・ユーザー成果物をコミットせず、draft Skillを実運用しません。
変更後はlint、tests、repository validationを実行してください。
