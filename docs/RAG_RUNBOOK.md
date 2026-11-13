# Identity-scoped local-model RAG API

This describes the CORE05 API extended by the [CORE08 operator workspace](OPERATOR_WORKFLOW.md).
The original `/baseline` HTML and `/chat` API remain hash/extractive. Identity-bound
reviews/tickets use `/rag/*`; there is no complete-core tag yet. Python 3.12, local Ollama and PostgreSQL 17 with
pgvector are the full demo stack. SQLite exact scan is a labelled fallback.

## Setup

Install `requirements-dev.txt`, run `python manage.py keys` once, and keep the
ignored `.env`. Start PostgreSQL with `docker compose up -d --wait`. Set
`DATABASE_URL` locally to that service using its generated password; never paste
it into an issue or commit it. Do not run migrations/tests against private data.
The checked migration 3 adds identity/grant/trace tables; migrations 1/2 are unchanged.

Download public local models explicitly:

```sh
ollama pull nomic-embed-text:v1.5
ollama pull qwen3.5:9b
python rag_manage.py identities
python rag_manage.py seed
python index_worker.py run --backend ollama
```

Identity setup generates Alice/Bob users, operator and admin in ignored
`credentials.json`; raw tokens are never printed and only SHA-256 hashes enter
the DB. Reruns retain the same tokens and reconcile interrupted setup, refusing
mismatched files. Restrict access to the local file (POSIX mode 600 on creation).
The seed command queues two explicitly synthetic notes, gives Alice/operator
the repair note and Bob the restricted fictional marker note. Grants are explicit;
operator/admin roles do not implicitly grant retrieval access. Existing knowledge
documents have no RAG read grants after migration until an admin grants them.

In another terminal, set `FIELDDESK_RAG=ollama` and run `python run.py`.
PowerShell: `$env:FIELDDESK_RAG = 'ollama'`. Model setup failure keeps the API alive
with safe RAG handoffs; fix model availability and restart. A runtime outage is
retried on the next request. `FIELDDESK_GENERATION_MODEL` optionally selects another
installed model; its digest is recorded, never mixed with embedding spaces.

Use `/docs` or an HTTP client with `Authorization: Bearer <identity token>`.
The new tokens work only on `/rag/*`. Legacy shared keys work only on the original
baseline routes and cannot authenticate to RAG. Legacy and new corpora are separate.

| Endpoint | Scope |
| --- | --- |
| GET `/rag/me` | Current identity/DB role |
| POST `/rag/chat` with `{"message":"What must I record in a projector repair ticket?"}` | Any enabled identity; explicit grants determine evidence |
| GET `/rag/chunks/{chunk_id}?space_id=...` | Current permitted ready source only; unknown/denied returns 404 |
| POST `/rag/documents` | Operator/admin; same source_key/title/body/space_id model as knowledge ingest |
| GET `/rag/jobs/{id}` | Operator/admin; bounded job metadata, no lease token/source body |
| PUT `/rag/documents/{doc}/grants/{identity}` with `{"enabled":true}` | Admin only |
| PUT `/rag/documents/{doc}/enabled` with `{"enabled":false}` | Admin soft-disable; suppresses retrieval/source reads |
| GET `/rag/traces/{id}` | Owner only; others return 404 |

## Permission and generation boundary

The PostgreSQL query joins enabled identity, explicit document grant, enabled
document, selected embedding space and current ready target **before** ordering by
pgvector cosine distance. It retrieves at most three chunks above a fixed 0.35
similarity threshold. SQLite applies the same SQL permissions, then exact cosine
scan. The threshold is a development heuristic, not a calibrated accuracy claim.
Queries use `search_query:`; indexed chunks use `search_document:`. Space IDs bind
model, digest, dimension and chunking version. A changed model fails closed.

Permissions/ready pointers are rechecked before prompting and again for each
returned citation in the final assembly transaction. PostgreSQL shares row locks
on identity/grant/document/target during that final check. Disabled or replaced
evidence suppresses the entire answer. The previous ready version remains usable
while a replacement is still indexing; it stops being retrievable after publication.

Generation uses loopback HTTP only, a 90-second client deadline, one concurrent
generation per app process, context 4096, maximum output 400 tokens, temperature 0,
seed 7 and thinking disabled. A conservative 2,800-byte UTF-8 prompt cap prevents
unbounded Unicode input/context truncation; oversized prompts hand off. This does
not claim a cross-process GPU quota or guarantee server cancellation after timeout.
No model tools/actions are registered. Document text is marked untrusted data.

Structured output must include an answer and 1–3 unique supplied chunk IDs with
exact contiguous quotes, each 12–600 characters. Quote offsets are source Unicode
character positions. A request-specific JSON grammar permits only supplied IDs
and exact source sentences/full chunks, reducing copying errors in small models.
The server still independently validates every returned ID, quote and grant.
The grammar permits two outcomes: an answer with at least one citation, or an
empty handoff. This prevents a small model's observed `handoff=false`/empty-citation
combination without changing the service's fail-closed validation rules.
Invalid IDs/quotes, changed access/version, malformed/truncated
output, model change, timeout/outage or no supported evidence return a generic
handoff without draft text/citations. Exact quote matching proves source membership;
it does **not** prove every generated statement is semantically supported. Review
actual demonstration answers manually and keep later held-out evaluation separate.

Traces persist identity/space/outcome, elapsed time, source IDs and model/token
counters. They do not persist query text, prompts, answer text, source quotes or
credentials. Unanswered query review persistence/retention is CORE08/later work.
The existing operator-approved ticket workflow remains separate; RAG cannot approve it.

## Reproducible checks and actual evidence

```sh
python -m pytest -q
python evaluate_retrieval.py
python demo_rag.py --output artifacts/local-rag.json
python demo_rag.py --postgres --output artifacts/pgvector-rag.json
```

The demo ignores `.env`, uses generated temporary identity tokens, a disposable
SQLite file or its own uniquely named pgvector container, and public synthetic
fixtures only. The container is stopped on exit. Reports include actual model
digests, settings, answers/citations, counters and permission/outage results.
The 1ms timeout case is an actual Ollama transport deadline, not a mocked adapter.
The unavailable-endpoint case uses a reserved loopback port without stopping a
shared Ollama server. Neither case presents an invented fallback answer.

11 November local report: nomic 768 dimensions + qwen3.5:9b on Windows/RTX4070,
**SQLite exact scan**, answerable/unsupported/restricted/outage/timeout/revoked
cases passed. The answer correctly states room code and fault description, and
quotes exactly the supporting source span. This is one manually inspected
synthetic answer, not an accuracy score. Local Docker returned engine errors;
PostgreSQL evidence must come from the separate actual-model CI run.

CI's actual-model job uses Linux/CPU Ollama 0.40.16, nomic and qwen3.5:9b;
its exact digests/counters are recorded separately in the `actual-pgvector-rag`
artifact and job log. Do not attribute CPU latency/counters to the local GPU run.
The 0.6b model failed citation/routing checks; a local 1.7b candidate handed off
the answerable case. Neither is counted as a passing demonstration. CI therefore
uses the default 9b model that passed the complete local scenario. The
local 0.17.7 runtime retains an older installed 9b digest; the current registry
tag requires a newer runtime, so CI pins 0.40.16 and records its actual digest.
The ordinary SQLite/PostgreSQL jobs use deterministic adapters. The original
16-case diagnostic remains exactly unchanged: 9/10 top-1, 4/6 unsupported handoffs,
18/18 source-matching quotes, three known misses. These are baseline diagnostics.

Permission tests cover unauthenticated/invalid/legacy tokens, user ingest/job/grant
and disable denials, cross-owner sources/traces, wrong spaces, client impersonation,
no implicit elevated-role read and revocation/version changes during generation.
The larger held-out quality gate, operator/review UI, screenshots, complete ticket
scenario and clean-clone final acceptance are still pending.
