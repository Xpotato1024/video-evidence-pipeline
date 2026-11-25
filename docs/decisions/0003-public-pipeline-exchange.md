---
schema_version: "1.1"
document_id: VEP-ADR-0003
title: "Public acquisition, clarification and output contracts"
document_type: architecture-decision-record
status: proposed
canonical: false
canonical_for: null
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents: ["VEP-ADR-INDEX", "VEP-ADR-0001", "VEP-ADR-0002", "VEP-OPS-PUBLIC-WORKFLOW"]
---
# Public acquisition, clarification and output contracts

Proposed in issue #83. See the [public workflow quickstart](../operations/public-workflow.md).

## Context

Public users and AI clients can start with local files, a source URL, or a saved manuscript. They may use an interactive browser, a local model, or an external AI. A resumable interface needs to distinguish a recoverable interruption, a user action and an access denial. A short meeting note and a verified recording can inform different claims. Users also need to choose a document kind and an output independently.

## Decisions

Use the existing CLI and JSON exchanges as the stable boundary. A human authenticates with the provider when the adapter asks for it. The provider runtime owns credentials and ephemeral locators. The job owns the verified source and the progress needed to resume. A separate delegated OAuth integration would require provider support and client registration.

Route segmented acquisition through the existing pinned media backend and keep its quotas and source validation. An incomplete transfer cannot supply canonical media. Persisting partial bytes across refreshed manifests would require evidence that they belong to the same rendition and segment set. Verified whole-source reuse requires the existing artifact contract.

Accept informal notes in the same input inventory and preserve their origin. The relevance of a source is decided per statement. Record unresolved questions, why they matter, and any user or documentary clarification as separate evidence. Historical evidence and prepared requests retain their original fingerprints. Educational supplements retain their own attribution.

Separate authoring presets from rendering targets. Generic rendering reads verified approved Markdown and records the derived bytes. The lecture renderer takes a requested set of formats and binds review to those files. The established full lecture bundle remains available for existing callers. Rendering reuses the saved manuscript with the appropriate format dependencies.

## Alternatives considered

Per-agent instructions could teach each AI to interpret free-text errors, find the media and choose output artifacts. Each client would then own provider behavior, failure recovery and the output contract. A single shared CLI protocol puts the state transitions and verification in VEP and leaves interpretation and drafting to the agent.

A global rank for source types would be simpler to configure. It would also misrepresent which source establishes the point at issue. The selected policy retains the evidence behind each conclusion and supports corrections without promoting tentative notes to confirmed statements.
