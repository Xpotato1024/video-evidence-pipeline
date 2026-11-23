---
name: draft-technical-lecture
description: Verified lecture evidence into a structured, source-linked teaching draft.
---
# draft-technical-lecture

Use this draft procedure to evaluate the lecture exchange contract. The registry controls production
activation. The procedure applies only after VEP freezes the source and response contract.

## Inputs and outputs

Read the frozen lecture request, scoped authoring profile and `rich-content.schema.json`. Map definitions,
derivations, validity conditions, teacher emphasis, assignments, worked examples, and links to prior/follow-on
sessions before composing blocks. Preserve all explicit emphasis. Mark unsupported coverage unresolved.
Prioritize current evidence on differences in notation or convention. Label prior context and supplementary
reasoning separately and retain the exact source references. Put symbols and assumptions next to each equation.
Use the profile's calculation rules in ordered worked steps. In submission mode, retain the reasoning and
calculation needed to answer the assignment in the required format.

## Validation and stop conditions

Cross-check the draft's wording, math, data and figures against current and previous sources.
Stop if request identity, source access, or the required independent context cannot be verified.

## Boundaries

Do not invent facts, override evidence with an example, silently omit a critical step, or claim approval.
Use the public CLI for deterministic validation and writes.

## Reader-facing hierarchy

Use the lecture objective and documented emphasis to choose which operation to explain in depth. Show the assumption that eliminates a term or the boundary that fixes a constant beside the equation. Attribute emphasis to the instructor only when the evidence supports the attribution. For symbolic algebra, author one equation block with an aligned chain for each reasoning move. Reserve substitution, intermediate precision and rounding fields for actual numerical exercises. Put optional invented examples after the lecture thread and identify them there. Keep exact evidence on individual blocks, figures, equations and steps; use section endnotes in the reading view.
