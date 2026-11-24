---
schema_version: "1.1"
document_id: VEP-ADR-0002
title: "Lecture production, caller policy and immutable exchange"
document_type: architecture-decision-record
status: proposed
canonical: false
canonical_for: null
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents: ["VEP-ADR-INDEX", "VEP-OPS-TECHNICAL-LECTURE"]
---
# Lecture production, caller policy and immutable exchange

Proposed in issue #81. The [operations guide](../operations/technical-lecture.md) describes the implemented CLI.

## Context

Previous guides were corrected in the final HTML. The next render lost those corrections, while the caller repeated design criteria in Python and authoring instructions. A second implementation of drawing and mathematical notation would require auditing each view separately.

## Decision

VEP owns source verification, the typed lecture exchange, portable source import, rendering, review validation and human approval receipts. Its public interface is the versioned JSON exchange and CLI. The calling application owns the profile, private manuscripts, enrolled sessions, storage, delivery permissions and learner records.

A new request freezes the generic [authoring, visual planning and visual audit criteria](../../config/lecture-guidance.yaml) alongside the supplied profile. The three evaluated role adapters refer to the same criteria. Preparing a request does not execute any adapter. An independent auditor inspects rendered outputs and receives the criteria for the relevant review. A CLI pass establishes identity and structural consistency. A human approves the exact review target.

One typed manuscript produces Markdown, editable MDX, standalone HTML, PDF and static site files. Reused geometry is declarative and bounded. Imports retain source references and version identity. The caller can bind private page aliases before rendering using the frozen catalog. Course material and provider destinations stay in the caller.

## Alternatives considered

| Option | Consequence |
| --- | --- |
| Separate HTML generators for each caller | Reimplements rendering, cross-references, print layout and source verification across courses. |
| Put course state and solution rules in VEP | Couples a public evidence pipeline to private records and assessment decisions. |
| Use HTML or unrestricted executable MDX as the interchange | Loses typed provenance or requires executing manuscript code to recover it. |
| Use typed source with constrained MDX import | Keeps one reusable rendering path and permits audited text and formula corrections. Structural edits require the typed source. |

## Compatibility and verification

Historical v2 requests omit the guidance field. Validation preserves their original fingerprints, audit scopes and approval targets. Re-preparing an existing version uses its frozen criteria. A new version picks up the current criteria. Old v1 evidence archives remain valid inputs. The renderer records its component file digests. Node dependencies, browsers, fonts and PDF utilities are deployment dependencies.

Exercise both source routes and fresh destinations; verify referenced equations, accumulated construction stages, source crops, portable edits, the actual PDF and narrow-screen HTML. Check existing approved bytes and caller learning records independently. Publishing or hosting is a separate authorized action.
