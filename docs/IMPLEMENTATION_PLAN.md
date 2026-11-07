# FieldDesk — December 2026 implementation plan

## Immediate core milestone — 17 November 2026

The core is needed for resume demonstrations within the next ten days. On 8,
11 and 14 November build migrations/chunks/jobs, actual local embedding/LLM
retrieval in PostgreSQL/pgvector, permission filtering, source-validated answers,
review queue/interface and the existing approved-ticket workflow. Include real
model evidence, restart/idempotency checks and eight permission-denial cases.
Record the 16-case diagnostic honestly. The larger held-out evaluation and full
v1 quality/hardening gates follow after the core, through December. Final core
acceptance is 17 November; mocked inference alone does not satisfy it.

## Source-grounded starting point

Reviewed main `52824bb4892e5fe690db34d1b0de06e953f96c51`: `app.py`, settings,
management/seed commands, interface, Compose, test/CI definitions and evaluation.
The current app uses shared user/operator keys, deterministic 256-dimensional
hash vectors, exact excerpt answers and optional pgvector storage. Actions are
approval-gated and idempotent. There is no local LLM, versioned chunk corpus,
document-level permission model, separate index worker or review queue yet.

The existing 16-question development diagnostic records 9/10 top-1 retrieval,
4/6 appropriate unsupported/escalation handoffs and 18/18 source-matching quotes.
Quotes matching their source do not establish question answering quality. Keep
this baseline and its three misses for comparison.

## Bounded v1 architecture

Keep Python/FastAPI and the existing interface; split modules by API, persistence,
retrieval, generation, authorisation and jobs rather than rewriting the frontend.
Make PostgreSQL/pgvector the full-feature v1 storage. Retain the SQLite/hash mode
as an explicitly labelled offline demo and deterministic test baseline.

Add versioned migrations and records for identities/roles, documents, immutable
document versions, chunks with source offsets, access grants, embedding spaces,
index jobs, chat/review references and existing approval actions/tickets. The
embedding space stores model identifier/digest, dimensions and chunking version;
never mix existing 256-dimensional hashes with learned vectors.

Use a local Ollama embedding adapter and a small local generation model selected
after a hardware probe. Record exact model identifiers, dimensions and settings
and require an explicit model-download setup step. Set input/output budgets,
timeouts and cancellation. Default tests use deterministic adapters; a separate
real-model acceptance run is mandatory. No paid inference service is required.

The indexing worker claims durable jobs transactionally, uses leases and bounded
retries, and resumes after restart. A job is identified by document-version and
embedding-space, so retries cannot create duplicate chunks. Mark a version ready
only after its full index succeeds; keep the previous ready version queryable
during a replacement. Deleted/revoked versions cannot remain retrievable.

## Permission and answer boundaries

Use generated local identities with user/operator/admin roles. Bind requests to
an identity, not a client-supplied role. Filter access grants in the database
before similarity search and before placing text in the prompt. Recheck current
version/revocation at answer assembly. Test direct source URLs, review queues,
traces and citations for cross-role leaks as well as the search endpoint.

Generation receives only permitted retrieved chunks. Its structured result must
name source/chunk IDs and quote supporting spans. Validate IDs and quotes against
that request's authorised evidence; unsupported output becomes a handoff. Model
output cannot approve tickets, execute document instructions or call arbitrary
tools. Preserve explicit operator approval and transactional ticket creation.

Persist unanswered/low-support queries in a bounded operator review queue with
states `open`, `in_review`, `resolved`. Store only the opted-in/synthetic query
content needed for review, with identity scope, retention/deletion controls and
no authentication secrets. Traces retain identifiers, model/token counters,
durations and outcomes without full prompt logging by default.

## Task sequence and acceptance

| ID | Work unit | Observable acceptance |
| --- | --- | --- |
| F01 | Modular app and migrations | Existing tests/baseline still run; upgrades preserve current demo data |
| F02 | Document versions and chunk/source records | Deterministic boundaries/offsets; duplicate ingest is stable; version replacement is explicit |
| F03 | Durable indexing jobs | API returns job status promptly; restart/retry produces one complete ready version |
| F04 | Identity and document permissions | At least eight cross-role tests deny retrieval, source and queue leaks |
| F05 | Local embedding adapter and space metadata | Real embedding vectors fit declared dimensions; model switch triggers separate reindex |
| F06 | Bounded local generation with citation checks | Unsupported/invalid citations hand off; timeout is visible; no automatic actions |
| F07 | Persistent operator review queue | Queries can be triaged/resolved and survive restart without privilege leaks |
| F08 | Ingest/version/job/review interface | Operator can upload approved text, follow indexing and resolve an unanswered question |
| F09 | Expanded labelled evaluation | Preserve 16-case baseline; add 40 cases split 24 development/16 held-out before tuning and record corpus/version |
| F10 | Actual local-model demonstration | Ingest, answer, deny restricted evidence and handle model outage on the documented hardware |
| F11 | Worker recovery and reindex drills | Kill/restart worker, expire a lease and repeat ingest without duplicate ready chunks |
| F12 | Privacy, quotas and observability | Size/context limits, retention and secret-free diagnostics tested; actual token counters recorded |
| F13 | End-to-end user/operator scenario | Answer -> handoff -> review -> approved ticket workflow passes with PostgreSQL |
| F14 | Clean-clone deployment | App/worker/database start with generated credentials; local model setup is explicit and health-checked |
| F15 | Release evaluation and runbook | Security/evaluation gates pass; model/dataset/hardware and known misses documented |

## Evaluation and release gates

Use at least eight held-out answerable and eight held-out unsupported questions
within F09's 16 held-out cases, plus the eight permission-denial cases. Target at
least 7/8 hit-at-3 for answerable and 7/8 correct handoffs for unsupported cases,
100% validated citation IDs/quotes, and zero permission leaks. These are release
targets, not current results. Also manually assess support/relevance of ten
generated answers; lexical quote checks alone cannot prove grounding.

Record retrieval and generation separately, confidence/handoff decisions,
model/digest, corpus/chunk versions, p50/p95 latency and actual token usage on the
test hardware. Do not tune on held-out labels; replace contaminated holdouts.
If gates fail, keep the release candidate and fix the cause rather than changing
labels to make a pass. ANN indexing is unnecessary at this corpus size.

`v1.0.0` requires F01–F15, passing SQLite and real PostgreSQL CI, actual local-model
evidence, restart/reindex checks, runbook and screenshots. Single-workspace v1
does not claim enterprise tenancy, customer use or a real external ticket provider.

Adapter reference: [Ollama embedding API](https://docs.ollama.com/api/embed).
