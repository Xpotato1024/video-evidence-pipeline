# Repository instructions

## 実装・検証・レビューの運用

通常実装は一人のprimary agentが、調査・設計・実装・変更に必要な検証・失敗の修正・文書同期まで一貫して担当する。repository側では特定modelやmodel間の担当分担を固定せず、model選択はsystem / developer instruction、Codexのユーザー設定・実効設定、ユーザーの明示指定に従う。独立レビューは重要な設計判断、高リスク変更、明示gateなど実質的な独立性が必要な場合にだけ追加する。

有効な同条件の検証は再利用し、不完全レビューは不足部分を補完する。環境障害は失敗箇所から再開する。全面実操作E2Eはリリース前の統合段階、変更に必要な検証はその場で行う。関連Skillから古い担当指定・追加の全面再実行義務を持ち込まない。条件・独立性・権限境界の正本は[実行ワークフロー](.agents/policy.md)。

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

このファイルは、`AGENTS.md`を自動検出するAI向けの薄い入口です。詳細規則の正本は
[`.agents/policy.md`](.agents/policy.md)、Skill運用の正本は
[`.agents/governance.md`](.agents/governance.md)です。

作業開始時に、対象Issue／PR、`docs/registry/documents.yaml`、該当canonical文書、
`.agents/policy.md`を確認してください。

必須事項:

- 認可されていない動画を取得しない。
- Cookie、token、署名付きURL、実動画、音声、字幕、抽出フレーム、ユーザー成果物をVEP開発リポジトリへコミットしない。別の認可済み非公開リポジトリへのstudy-bundle保存は`.agents/policy.md`と`docs/operations/study-bundle.md`に従う。
- raw sourceを上書きしない。未実行・不明・低確度を推測で補完しない。
- authoringとindependent auditを同一コンテキストで完結させない。
- Issue-first、Draft PR、明示依頼なしのReady化／merge／Issue closeを行わない。
- 文書追加・移動・正本性・`metadata_mode`変更時は文書台帳を同じPRで更新する。
- registry-only文書へ汎用YAML front matterを追加しない。
- 変更範囲とfailure modeに必要な検証を選ぶ。具体的な選択・証拠再利用・環境障害からの再開は`.agents/policy.md`を正とし、変更前後の全件再実行を要求しない。

Skillは明示的に選択し、`lifecycle = "draft"`のSkillを実運用として実行しないでください。
