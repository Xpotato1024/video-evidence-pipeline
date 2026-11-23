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

Open a study guide with a compact map of the problems, decisions and results. Link directly to the relevant sections using `[label](#block-ID)`. Keep the main workflow visible and make each derivation a separately addressable heading. Mark the decisive operation with `**short emphasis**`. Use the identical TeX in `\(...\)` for a quantity in prose, tables, assumptions, symbol definitions, step rationales and captions. For example, the vector `\(\mathbf e_r\)` must agree with the displayed equation. Introduce symbols locally and avoid repeating long glossaries. Use short comparison tables when assumptions or actions differ. Verify the page by starting at a mid-lesson question.

Use a bullets block for a set with no required order. Use Steps/Step for dependent operations: name the action, explain the decisive reason, place the supporting expression with that step, and show the result where needed. A single aligned expression can carry a whole symbolic transformation. Preserve a comparison table when the row/column relationship matters. Let the diagram or equation show what it can; put the applicable condition and the reason for a choice nearby. At the end, select the few relationships the reader should be able to explain in their own words.

Before the first use of a symbol, state what it denotes and why the reader needs it. Distinguish a physical quantity, a coordinate, a component and a direction. Keep a concrete example and the takeaway visible. Use a question-titled background block for prerequisite reasoning that a prepared reader can skip. Bind supplementary explanations to their actual provenance.
