---
schema_version: "1.1"
document_id: VEP-OPS-STUDY-BUNDLE
title: "Portable study-bundle export"
document_type: operations
status: active
canonical: true
canonical_for: study-bundle-export
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-OPS-CLI-LOCAL
  - VEP-ARCH-ARTIFACTS
  - VEP-AGENT-POLICY
---
# Portable study-bundle export

`study-bundle` is an explicitly authorized archival view for a separate private learning repository.
It is not a resumable VEP workspace, does not run an AI provider, and never performs Git operations.
The approved assignment remains `output.format: markdown`; the export format selects packaging only.
Both `HUMAN_APPROVED` and `EXPORTED` jobs are accepted after deep upstream and approval verification.
Unapproved jobs are rejected. Export neither creates approval nor changes the original job state.
Existing `vep export WORKSPACE` Markdown export and its state transitions are unchanged.

## Commands

Create the destination parent first, but leave the destination itself absent.

```text
vep export WORKSPACE --format study-bundle --destination /private/study/courses/COURSE/sessions/S01/vep/R001 --confirm-private-export --json
vep bundle-validate /private/study/courses/COURSE/sessions/S01/vep/R001 --json
```

`--confirm-private-export` acknowledges the data-copy boundary; it is not human content approval.
Use a destination outside the VEP repository. Links and junctions in destination paths are rejected.
A byte-identical existing valid package is reused; other existing content is never overwritten.
A sibling lock serializes VEP writers. Files are staged and checked before exclusive destination creation.
The manifest is published last. An interruption during final publication may leave an incomplete directory;
validation rejects it. Inspect it and use a new destination rather than forcing an overwrite.
Ignore `.vep-study-*` staging and `.*.vep-export.lock` lock files in the receiving repository.

## Contents and references

`summary.md` is byte-for-byte the approved Markdown. `bundle-manifest.json` lists hashes, sizes,
entrypoints, approval binding, VEP package version and omitted source identities.
Raw/refined transcript segments, refinement edits/ambiguities, evidence/ambiguities, supporting
PDF/image/notes and rendered pages, frames, selected draft history, claims, requirements,
audit findings/comparisons, human-review bundle and the existing approval ledger are included.
The contents use their original workspace-relative paths, now relative to the package root.
The `source-index.json` catalog resolves source IDs and transcript segment fingerprints to local payloads.
Artifact references and semantic source references are checked transitively; an unresolved non-media artifact stops export/validation.
`provenance.json` keeps assignment and producer metadata without runtime source locators.
`.gitattributes` disables Git text conversion inside the package to preserve all checksums.

Input sources (including supplied transcript input files) and normalized A/V are not copied.
Raw transcript *segments* remain included, including when the input was a supplied text file.
Omitted source paths/hashes identify provenance, not a required readable external location.

## Privacy, integrity and limits

Do not commit real jobs or real artifacts into the VEP development repository. An owner may explicitly
archive this selected package in a separate authorized private learning repository. No Git push is automated.
Network source manifests, job YAML, authorization records, credentials, requests/responses, logs, search
cache, models and runtime configuration are outside the export allowlist.
Source content and reviewer identity are preserved, not automatically redacted. Check course permissions
and the actual content before storing or sharing it; private Git storage is not external-AI authorization.

The offline validator requires only installed VEP code, not the source workspace, A/V or a model.
It checks manifest fingerprint, exact inventory, file hashes/sizes, relative references and link safety.
Checksums detect damage relative to the stored manifest, not malicious replacement of an entire archive.
The original CLI-asserted reviewer is not a cryptographically authenticated signature.
Without original A/V, re-listening and ASR reprocessing are impossible; the package records this limitation.

Limits: at most 10,000 included files, 512 MiB per included file, 2 GiB total payload,
8 MiB manifest and 64 MiB per structured metadata file. Large Git objects may require separate storage policy.
For restoration, clone the learning repository to a new path, run `vep bundle-validate`, open the PDF assets
and read the summary/transcripts without starting the original job. Do not edit a completed package;
store subsequent processing as a new package such as R002.
