---
schema_version: "1.1"
document_id: VEP-CONCEPT-SEMANTIC-QUALITY
title: "Semantic quality principles"
document_type: concept
status: active
canonical: true
canonical_for: semantic-quality-principles
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-README
  - VEP-OPS-AI-EXCHANGE
  - VEP-ARCH-PIPELINE
  - VEP-GOV-AI-INSTRUCTIONS
---
# Semantic quality principles

この文書は、VEPのsummary、report、meeting-style outputにおけるsemantic selectionとquality reviewの設計原則を、公開研究と既存のagent Skill設計から短く整理したものです。研究sourceは実装のruntime dependencyではありません。production Skillは自身の契約をself-containedに保持し、この文書が存在することを前提に実行してはいけません。

## VEP policy

### Selection before compression

要約を「全文を一様に短くする」処理として扱いません。次の順で考えます。

1. assignmentの`purpose`、`audience`、requirementsを確定する
2. source evidenceからassignmentに関係するsemantic unitを選ぶ
3. core、supporting、omittedを区別する
4. 選択済みの内容だけをcompress／fuseする

topic数、発言時間、evidence件数、entity件数、number件数、durationに比例して文字数や項目数を配分しません。全topicを等しい重みで列挙することも、transcript順に一文ずつ縮めることも品質目標ではありません。

### Faithfulness and salience are separate

- **Faithfulness**: outputのclaimがsource evidenceに支持され、関係や条件を捏造していないか
- **Salience／selection**: assignmentに必要なcore pointを残し、周辺detailで埋めていないか

すべてのclaimにrefが付いていても、中心命題・結論・安全条件・期限・禁止事項を落としていればselection failureです。逆に、重要そうに見える情報でもsourceにない推測はfaithfulness failureです。auditはこの2つを別の観点で確認します。

### Intent-dependent selection

同じtranscriptからでも、一般summary、examples-focused task、operational notice、meeting minutes、technical reportでは残す情報が変わります。`--purpose`と`--audience`はprompt上の飾りではなく、evidence selectionの入力です。

`role:decision`、`role:action-item`、`role:deadline`、`role:condition`、`role:example`などのinformation roleは、source上の役割を表します。roleをtask-independentな`importance=0.8`へ置き換えません。役割が重要かどうかはassignmentとaudienceの組合せで決まり、roleだけから人や期限を推測してはいけません。

### Concrete anchors without stuffing

abstractな言い換えだけでは、後から事実を確認できません。選択済みのcore pointを識別・理解するために必要な少数のconcrete anchor（数値、日付、固有名、threshold、代表例、option名など）は保持します。

一方、「具体的に見せる」ために無関係な数字やentityを詰め込みません。sourceにないowner、deadline、numberを補完せず、短いsummaryをentity一覧へ退化させません。anchorの選択はsource evidenceとassignmentに束縛します。

### No single-gold wording

良いsummaryやreportは、同じsemantic unitsを異なる自然な文で表せます。評価はreference textの完全一致ではなく、assignment-critical units、role coverage、concrete anchors、omission correctness、unsupported claimの有無、coherence、non-redundancyで行います。

## Source → adopted principle → VEP application → not adopted

| Source | Adopted principle | VEP application | Not adopted |
| --- | --- | --- | --- |
| [Agent Skills specification](https://agentskills.io/specification) | Skillは`SKILL.md`を中心に、必要なresourceを段階的に読み込む自己完結した単位にする | request、frozen Skill snapshot、response schemaを明示的に束ね、production Skillは自分の契約を持つ | 外部clientの自動dispatchや、docsをruntime入力にする依存 |
| [OpenAI `skill-creator`](https://github.com/openai/skills/blob/main/skills/.system/skill-creator/SKILL.md) | conciseなinstructions、適切な自由度、progressive disclosure、検証可能なworkflow | Skill本文を短く保ち、deterministic処理はexecutor／schemaへ分離し、active Skillの検証を要求 | Skill作成Skillをproduction outputの品質判定器にすること、暗黙の権限付与 |
| [OpenAI `notion-knowledge-capture`](https://github.com/openai/skills/blob/main/skills/.curated/notion-knowledge-capture/SKILL.md) | capture前に目的・audienceを明確にし、context・source link・ownerを構造化する | `--purpose`、`--audience`、requirement map、source refsをrequestへ固定する | Notion API／MCP、外部workspaceへの自動write |
| [OpenAI `notion-meeting-intelligence`](https://github.com/openai/skills/blob/main/skills/.curated/notion-meeting-intelligence/SKILL.md) | meeting goal、attendees、decision、timeboxに応じてpre-read／agendaを調整する | meeting-style outputではdecision、action、unresolved questionをassignmentに応じて選ぶ | Notion context取得、固定agenda template、外部参加者への自動公開 |
| [Anthropic skills repository](https://github.com/anthropics/skills) | specialized workflowをself-contained Skillとして提供し、例と実装の境界を明示する | Skill snapshot、fresh context、resource provenanceをproduction exchangeの境界にする | vendor固有のruntime、license、automatic activationをVEP契約へ持ち込むこと |
| [GitHub Copilot meeting-minutes Skill](https://github.com/github/awesome-copilot/blob/main/skills/meeting-minutes/SKILL.md) | 短いmeetingでもdecisionとaction itemを優先し、owner／due dateを記録する | sourceに存在するdecision、action、owner、deadlineをrole付きevidenceとして選ぶ | 全meetingを固定60分以下とする制約、attendee／agendaの固定テンプレート |
| [SummEval (TACL 2021)](https://aclanthology.org/2021.tacl-1.24/) | 自動metricだけでなく、人間判断を含む複数のquality dimensionを分けて評価する | faithfulnessとsalienceを別のaudit観点にし、human reviewをterminal gateにする | ROUGEだけでpass／failを決めること、研究datasetをproduction evidenceとみなすこと |
| [Pyramid method (N04-1019)](https://aclanthology.org/N04-1019.pdf) | summaryには単一の最良表現がなく、content unitの選択を評価する | no single-gold wording、assignment-critical semantic unit、omission correctnessを採用する | 全sourceに共通する固定importance score、Pyramidの数値をVEPの唯一のgateにすること |
| [QMSum (NAACL 2021)](https://aclanthology.org/2021.naacl-main.472/) | 長いmeetingではquery／user information needに合わせてlocateしてからsummarizeする | purpose／audience／requirementsでevidence selectionを変え、generic summaryへのflatteningを拒否する | benchmark queryをそのままCLI契約にすること、1つのgeneric summaryを全読者へ最適化すること |
| [MeetingBank (ACL 2023)](https://aclanthology.org/2023.acl-long.906/) | meeting segmentとprofessional minutesの対応を研究対象として扱う | segment／timestamp refを保持し、最後にassignmentに応じてglobal salienceを選ぶ | segmentごとに同じ件数を書くこと、datasetのminutesをsource evidenceの代替にすること |
| [Abstractive Meeting Summarization survey (TACL 2023)](https://aclanthology.org/2023.tacl-1.49/) | conversation summarizationの課題、data、model、metricを別々に捉える | conversation typeとinformation roleをselection／auditの入力として扱う | surveyのtaxonomyをhard-codedなimportanceや一律output templateにすること |
| [Decision detection in multiparty dialogue (W08-0125)](https://aclanthology.org/W08-0125/) | meeting decisionは単なるtopicではなく、周辺dialogue actと文脈から同定する対象である | explicitなdecision、rationale、action、open questionを別semantic unitとしてsource refsへ結ぶ | discussion topic、参加者名、提案だけからdecisionやownerを確定すること |
| [Chain of Density (NewSum 2023)](https://aclanthology.org/2023.newsum-1.7/) | informativenessとreadabilityの間にはdensityのtrade-offがある | 選択済みcore pointを識別するconcrete anchorにbudgetを置き、entity stuffingを避ける | 固定CoD iteration回数、常にentity densityを最大化すること |
| [Controllable Abstractive Summarization (W18-2706)](https://aclanthology.org/W18-2706/) | userがlength、style、entityなどの高位preferenceを指定するとoutput focusが変わる | purpose、audience、requirements、lengthをrequestへ固定し、selectionを条件付ける | modelのcontrol variableをevidence semanticsと混同すること、ROUGE／F1を唯一の目標にすること |
| [EntSUM (ACL 2022)](https://aclanthology.org/2022.acl-long.237/) | entity-centric summarizationは指定されたaspect／information needに応じた制御問題である | assignmentが求める場合だけentityをconcrete anchor／selection focusとして扱う | entity、number、duration、topic、evidence countに比例した選択や文字数配分を既定にすること |

## Explicitly rejected shortcuts

VEPは次をsemantic qualityの正本にしません。

- 固定されたtask-independent importance score
- entity、number、duration、topic数、evidence件数に比例するselection／文字数配分
- sourceにないowner、deadline、number、引用、因果関係の推測
- Chain of Densityの固定iteration回数や、entity density最大化
- ROUGE-only、またはreference text完全一致だけのquality gate
- roleをimportanceと同一視すること
- source全体のtopic inventoryをsummary／reportの完成条件にすること

これらを拒否しても、具体性、salience、faithfulness、coherenceを捨てるわけではありません。assignment、evidence、source refs、ambiguityを束ねたrequestと、独立audit、人間reviewを組み合わせて判断します。

## Scope and maintenance

この文書は研究に基づく概念の正本です。実行時のSkill ID、lifecycle、eval、response schema、state transitionはそれぞれ`.agents/skill-system.toml`、Skill、`schemas/`、canonical code／docsが正本です。研究sourceの更新や新しいproduction契約は、この文書だけを変更して解決せず、対応するIssue／PRと実装・evalを同時に更新します。

## 関連文書

- [README](../../README.md): 利用者向け全体像とquickstart
- [AI exchange operations](../operations/ai-exchange.md): request／Skill／schemaの境界
- [Pipeline architecture](../architecture/pipeline-architecture.md): evidence-first pipeline
- [AI instruction governance](../governance/ai-instruction-governance.md): AI adapterとSkill SoT
