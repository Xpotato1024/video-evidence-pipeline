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

Open a study guide with a compact map of the problems, decisions and results. Link directly to the relevant sections using `[label](#block-ID)`. Keep the main workflow visible and make each derivation a separately addressable heading. Mark the decisive operation with `**short emphasis**`. Use the identical TeX in `\(...\)` for a quantity in prose, tables, assumptions, symbol definitions, step rationales and captions. For example, the vector `\(\mathbf e_r\)` must agree with the displayed equation. Group parallel definitions by subject. Put one symbol or related tuple, its meaning and unit on each line. Trace each introduced symbol to the nearby equation or labeled object where it is used. Draw a local reference for a direction, boundary or angle that cannot be located. Use matching math labels and arrows in figures; for a normal to the page, fix the viewing side and key the applicable circled dot (toward) or cross (away) once. Keep observational distinctions and causal reasoning in words. Use short comparison tables when assumptions or actions differ. Verify the page by starting at a mid-lesson question.

Use a bullets block for a set with no required order. Use Steps/Step for dependent operations: name the action, give the immediate instruction, place the supporting expression with that step, and show the result where needed. A single aligned expression can carry a whole symbolic transformation. Preserve a comparison table when the row/column relationship matters. Let the diagram or equation show what it can; put the applicable condition and the reason for a choice nearby. At the end, select the few relationships the reader should be able to explain in their own words.

Before the first use of a symbol, state what it denotes and why the reader needs it. Distinguish a physical quantity, a coordinate, a component and a direction. Keep a concrete example and the takeaway visible. Use a question-titled background block for prerequisite reasoning that a prepared reader can skip. Bind supplementary explanations to their actual provenance.

## First draft from lecture sources

Before outlining a study guide, walk the verified source timeline or the pages actually taught. For each
decision the learner must reproduce, map the source locator to an observable task and its planned block.
Look for a warning given once, a return to the same reasoning in different words, board work that expands
or checks a result, and a pause to let students solve a problem. A duplicate caption, a verbal tic, or
time spent waiting supplies no extra corroboration. Resolve vague slide pointers against nearby board work
or figures. If the frozen catalog lacks a verified key passage, request a new evidence preparation or
identify the unresolved gap. Keep editorial reconstruction and predicted exam content separate from
what the lecturer established.

For a study guide, mark the supported learner action beside the topic: recall, explain, calculate, draw or observe, with the concrete target. Keep it distinct from priority. Follow the context of instructions and evidence locators, including negations, reference-only material and optional reading. A board invitation to look at a transformation can ask the learner to understand or perform it; an experiment can ask for a visible change. Leave an unsupported action unmarked; never infer memorization or predicted exam coverage from repetition. Use existing heading, list or table blocks and bind them to the supporting evidence.

For each priority operation show the starting point, condition, decisive intermediate move and outcome.
Keep the decision and conditions visible and give each step a direct link to its reasoning. Present an in-class exercise's
givens and question before the hint, method table or solution. Supply a diagram when the learner needs
it to locate a boundary, interpret an observation or plot a computed point. Trace the source-to-block map
in reverse before handing off the first draft. A cited heading or final formula alone does not teach a move.
Keep the mapping in working notes and use the existing source references and coverage fields in the response.

For a reproducible calculation, derivation or drawing, show the problem before its solution, then identify the applicable equation. State the answer the student must produce (a value, a vector in a named basis, a distribution, or a drawing) and its given inputs. First display the target expression in a worked derivation after introducing its notation; keep unsolved exercises ahead of their answer. A slide reference is a citation; supply the necessary formula, data and assumptions within the lesson. Place the relevant figure immediately after a short prompt or action heading, then its caption and the paragraph that reads it. Match each visual interpretation to an actual mark or label. Introduce variables that belong only to the calculation near the calculation. Work backward from the answer. A new intermediate needs a meaning, a purpose and an identifiable next use. Prefer a direct route when it removes unnecessary symbols. Treat only given, already explained or source-supported recall items as known; point to earlier definitions where needed. At each non-obvious equality, keep the substitution, fixed quantities or operation needed to reproduce it on the main path. With reasons closed, check that a reader can write each next line from the preceding line and these prerequisites. Use a procedure block (kind calculation or drawing). In each operation give a short actionable title, an instruction in rationale, the independent display line(s) in expression when applicable, and the reason in explanation. The reading view puts reasons after the sequence and links each one back to its exact step. For drawings assign a distinct figure_id at every step; keep the axes, scale and previous marks, and show the newly drawn element. Put assumptions needed for the action above it. Underbrace or strike only the factor or term affected; attach the condition and a readable zero or cancellation label. Distinguish pairwise cancellation from individual zero terms.

Keep previously defined symbols and units in short, locally grouped bullets during drafting and revision. For two coordinate systems, boundaries or observations with a common question, use a comparison block with a title and two or more cases. Give each case a label and an items list in matching criterion order. Use a table when many rows need exact lookup.

Set a compact relation (including a vector sum) on one line. Start another display line at a substitution, differentiation, integration or other new operation. Only wrap a long sum if its measured width requires it. If the prose says to differentiate, substitute or divide and then gives the final form, show the equality immediately after the first of those operations. Keep only the fixed variables, operation or domain restriction in the accompanying text. For simultaneous relations or components with a shared operation, use a stretching left brace around aligned rows, for example \left\{\begin{aligned}a_x&=b_x\\a_y&=b_y\end{aligned}\right. Omit trailing row commas. Start the next operation in its own display; an equality chain has a different reading order from a pair of components. In a worked derivation where a factor drops or a term's origin matters, put a short annotation on that exact part of the displayed equation. For example use \underbrace{\cancel{\partial_t u}}_{\text{steady}:0} for a zero derivative, or mark the same factor in numerator and denominator for division. Retain the simplified equation, and give the condition locally. Distinguish cancellation of a common factor from addition of opposite terms. Inspect the reading view on a phone-sized viewport and in print: the annotations must be legible and directly attached to the intended symbols.
