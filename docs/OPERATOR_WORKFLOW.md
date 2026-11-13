# Operator workspace and human-approved tickets

This CORE08 workflow extends the existing Python/FastAPI/vanilla HTML application.
The original shared-key hash/extractive UI is preserved at /baseline; the new
root workspace uses identity bearer tokens and the separate learned corpus.
Neither UI stores tokens in localStorage/cookies. Disconnect clears private state
and aborts outstanding browser requests; untrusted text uses DOM textContent.

## Interactive setup

Python 3.12+, local Ollama and Docker Compose are needed for the pgvector path.
Use the README dependency setup, then generate local credentials once:

```sh
python manage.py keys
docker compose up -d --wait
```

Set DATABASE_URL locally to that generated PostgreSQL service (database fielddesk,
user fielddesk, loopback port 5438, password from ignored .env). Do not paste the
connection string into a report. Keep .env and credentials.json private.
SQLite is an explicit exact-scan fallback when DATABASE_URL is not configured;
it is not native pgvector evidence.

Install public models only if they are missing, then generate identity tokens,
seed synthetic notes/grants and start the worker:

```sh
ollama pull nomic-embed-text:v1.5
ollama pull qwen3.5:9b
python rag_manage.py identities
python rag_manage.py seed
python index_worker.py run --backend ollama
```

In a second terminal set FIELDDESK_RAG=ollama and run python run.py. PowerShell uses
$env:FIELDDESK_RAG = 'ollama'. Both processes must use the same DATABASE_URL.
Open http://127.0.0.1:8101 and connect with one generated identity token:

- admin: ingest text, inspect permitted jobs and grant readers by identity.
- operator: ingest/update explicitly permitted notes, follow indexing and review.
- alice: read the seeded repair note, ask questions and opt into a handoff review.
- bob: read the separate restricted synthetic note; cannot see Alice's reviews/tickets.

Only new sources grant the ingesting actor read access automatically. Existing
sources require an explicit enabled grant before an operator/admin can replace
their text. This check, new version/job creation and initial grant commit together.
Administrative role alone does not grant source reading. There is no public
deployment, multi-workspace tenancy or external ticket-provider integration.

## Walkthrough

1. As admin, enter a source key/title/text and queue it in a learned embedding
   space. Refresh jobs until READY; pending/running/failed are shown honestly.
   Repeating the exact ingest returns the same version/job. The separate worker
   owns embedding/model work, leases and retry behavior.
2. Grant Alice and the operator access to the document. Grants apply before
   retrieval/prompt construction and are rechecked at citation assembly.
3. As Alice, ask what to record in a projector repair ticket. Inspect the cited
   source with View permitted source. Source requests use the same identity.
4. Ask an unsupported pricing question. It hands off; no ticket appears.
   Explicitly check Share this question and Request operator review.
5. As operator, refresh the queue, claim that review, enter a human ticket title,
   propose it, then explicitly approve it. The requester can view the approved
   ticket; another user cannot. Repeated approval returns the same ticket.
6. Alternatively, enter a resolution note and resolve without a ticket. The
   requester can withdraw shared question text, including after ticket creation.

## Review contracts and retention

POST /rag/reviews requires an owned handoff trace, its exact normalized question
and JSON consent=true. Traces persist a query SHA-256, not raw query text.
Answered traces and changed/foreign questions cannot be turned into reviews.
No question is automatically stored in the review queue. Exact trace replay
returns one review. Each identity can have at most 20 open/in-review requests.

GET /rag/reviews lists at most 50 rows: users see their own; operators/admins see
the opted-in single-workspace queue. That shared query text never grants access
to a requester's source chunks or private trace. GET /rag/reviews/{id} follows
the same scope. User roles, enabled state and ownership come from the database.

States are open → in_review → resolved. Operator writes include expected_revision;
a stale edit returns 409. Claiming the same already-owned review is repeat-safe.
Only the assigned enabled operator may propose, resolve or approve. A requester
who later becomes an operator still cannot approve their own ticket.

Proposal and approval are distinct POST operations:
 /rag/reviews/{id}/claim, /proposal, /approve and /resolve.
A proposal stores one linked action in the existing actions table; approval
creates one existing-format ticket and marks the review resolved atomically
with its event. Duplicate/concurrent approvals produce one ticket/event.
Proposals expire after 15 minutes; explicitly re-proposing the same title refreshes
a still-active pending proposal. A changed title conflicts. No model calls these
routes or authorizes a ticket.

Question text expires after seven days and is redacted on the next review access.
DELETE /rag/reviews/{id} lets only its requester withdraw it earlier. Existing
approved ticket titles remain; the UI explains their separate retention.
For explicit local expiry cleanup run python rag_manage.py purge-reviews.
Events contain actor IDs, transition names and times, without question text.
There is no background scheduler/guaranteed wall-clock deletion while the app
is offline. Broader retention/deletion controls remain December hardening.

GET /rag/tickets/{id} returns a ticket only to its requester or an enabled
operator/admin in the trusted review workspace. Identity-linked actions/tickets
are excluded from legacy /actions and /tickets routes, preventing a shared
baseline key from bypassing the new identity boundary. Existing legacy-only
proposals/approvals and diagnostic behavior are retained.
The legacy knowledge API cannot overwrite identity-managed sources, and legacy
proposals cannot reserve the identity workflow's server-owned request-key namespace.

## Reproducible evidence

Install the browser-only verifier and run the isolated demo:

```sh
npm ci --prefix browser
cd browser
npx playwright install chromium
cd ..
python demo_workflow.py --postgres --artifacts artifacts/workflow
python demo_indexing.py --backend ollama --postgres --output artifacts/worker-restart.json
```

The demo generates private temporary credentials and owns a fresh disposable
pgvector container; it ignores personal .env/DATABASE_URL. It starts its own
loopback app and worker, drives actual Chromium UI/model calls, checks permission
denials and source quotes, injects a real failed loopback generation connection
and a 1ms transport deadline, then starts a fresh FastAPI app instance/listener
in the same demo process and compares exact persisted review/ticket/job state.
The separate worker drill below uses an actual crashed/replacement process.
Only synthetic fixtures are used. Logs redact generated
tokens before artifact publication. No browser tracing is enabled.

Without --postgres it owns a temporary SQLite database, explicitly labelled.
The separate worker demo terminates a process after claiming a lease, starts a
replacement worker with actual learned embeddings, and verifies exact replay,
version replacement, vector width and non-duplication. No mocked model adapter
counts as this inference evidence.

tests/test_workflow.py uses clearly labelled deterministic adapters for permission,
consent, quota, revision, expiration, atomic rollback and concurrency regressions.
The complete suite runs on SQLite and real PostgreSQL in CI. Original migrations
1–3 and the parsed 16-case diagnostic are preserved. Its known misses remain
9/10 top-1, 4/6 unsupported handoffs and 18/18 exact-source quotes; these are
baseline observations, not general answer accuracy. Final core acceptance is
still 17 November. Held-out evaluation and full v1 hardening follow in December.
