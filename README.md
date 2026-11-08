# FieldDesk

## December 2026 delivery

The current December v1 plan adds versioned knowledge, document permissions, durable indexing and an evaluated local-model path while retaining the working offline baseline.

See the [implementation plan and acceptance checks](docs/IMPLEMENTATION_PLAN.md).
The core demo target is **17 November 2026**; feature delivery and hardening
continue through **31 December 2026**. Completion requires verified evidence.

**CORE02 delivered:** versioned knowledge, exact source-offset chunks, durable
indexing jobs and a separate local-model embedding path. A real PostgreSQL/
pgvector run indexed 768-dimensional nomic vectors, recovered a crashed worker
process and replaced a version without duplicate chunks. Chat still uses the
original hash/extractive corpus; local-model answers and document grants are
remaining core work. See the [indexing runbook](docs/INDEXING_RUNBOOK.md),
[claim/evidence ledger](docs/CLAIM_EVIDENCE.md) and
[actual local-model readiness report](docs/model-readiness-2026-11-09.json).

[![Tests](https://github.com/apoorvkumarvcd23-coder/fielddesk/actions/workflows/tests.yml/badge.svg)](https://github.com/apoorvkumarvcd23-coder/fielddesk/actions/workflows/tests.yml)

A small service-desk prototype that finds cited operating instructions and
requires operator approval before creating a ticket. Designed for a campus
equipment desk: a user asks about a broken projector, finds the relevant note,
and proposes a repair request without letting a chatbot execute arbitrary actions.

**Python · FastAPI · SQLite · optional PostgreSQL/pgvector · plain HTML/JS**

![Local prototype showing a cited equipment-repair answer](docs/demo.jpg)

## Try it locally

Python 3.12+. No model account, GPU or paid API is required.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python manage.py keys
python manage.py seed
python run.py
```

Open **http://127.0.0.1:8101**. Copy `FIELDDESK_USER_KEY` from your ignored `.env`
into the access-key field. Ask **"How do I request a projector repair?"** and
inspect the source quotes. Propose a ticket, switch to the operator key, load
the operator queue and approve it. Repeating approval returns the same ticket.

Interactive API documentation: http://127.0.0.1:8101/docs. Use
`Authorization: Bearer <your-key>` on protected endpoints.

## Implemented

- Synthetic manual ingestion with content-hash deduplication.
- Deterministic 256-dimensional hashed bag-of-words retrieval with source IDs,
  titles and exact excerpt citations. Answers are extractive, not LLM-generated.
- Explicit human handoff and no-answer behavior.
- Separate user/operator keys; only operators ingest manuals or approve actions.
- Idempotent proposals, conflicting-key rejection, approval expiry and atomic
  ticket creation. Concurrent approvals create one ticket.
- Trace IDs, outcomes and elapsed time without storing chat text or keys.
- Local browser interface and automated failure/concurrency tests.
- Checked database migrations that adopt the old four-table layout without
  deleting documents or tickets; immutable versions and bounded Unicode chunks.
- Operator-only asynchronous knowledge ingestion/status/source endpoints, plus
  a separate worker with leases, fenced atomic publication and bounded retries.
- Separate model/digest/dimension/chunking spaces: learned vectors never reuse
  the existing 256-dimensional hash column. The old ready version remains active
  until its requested replacement finishes.

```mermaid
flowchart LR
    UI[Browser] --> API[FastAPI + key role]
    API --> R[Retrieve approved notes]
    R --> DB[(SQLite or Postgres / pgvector)]
    API --> A[Propose action]
    A --> P[Operator approves]
    P --> T[Atomic ticket creation]
```

## PostgreSQL / pgvector

SQLite is the zero-setup default. Its vectors are JSON and ranked in Python.
The PostgreSQL adapter stores `vector(256)` and runs exact cosine-distance
queries in PostgreSQL. Neither path pretends hash embeddings understand meaning.

```sh
docker compose up -d --wait
```

Add this line to `.env`, substituting the generated `POSTGRES_PASSWORD`:

```text
DATABASE_URL=postgresql://fielddesk:<your-generated-password>@127.0.0.1:5438/fielddesk
```

Run `python manage.py seed` and `python run.py` again. The health endpoint reports
`pgvector`. Keep `.env` private. `docker compose down` stops the optional DB.

## Tests

```sh
python -m pytest -q
```

Tests cover authentication/role boundaries, citations, unknown questions,
instruction-like document content, idempotent/conflicting proposals, concurrent
approval, expiry, missing tickets, trace privacy and persistence after restart.
CI runs SQLite and the real PostgreSQL/pgvector adapter. Locally, setting
`TEST_DATABASE_URL` runs the adapter suite against a **disposable** database;
tests truncate its four application tables. Never use a personal database.

New indexing checks also recreate their knowledge schema in a disposable
`*_test` database. They cover concurrent claims, process/lease recovery, source
offsets, model-space separation, failed publication rollback and version ordering.
CI additionally runs `python demo_indexing.py` (SQLite) and
`python demo_indexing.py --postgres` (a new disposable Docker database), both
with the deterministic hash adapter. Real Ollama evidence is recorded separately.

## Retrieval evaluation

```sh
python evaluate_retrieval.py
```

Evaluates 16 labelled synthetic questions through the chat API using a temporary
SQLite database, without changing your demo database or calling model services.
The current baseline retrieves the expected first source for 9/10 answerable
questions and correctly hands off 4/6 unsupported/escalation questions. All 18
returned citations match their sources, but correct quotes do not guarantee an
answer is relevant. See [the full evaluation and known misses](docs/retrieval-evaluation.md)
before comparing new embedding adapters. These are small development fixtures,
not production quality claims. `--fail-on-miss` enables a strict experimental gate;
the current baseline has known misses and intentionally fails that gate.

## Scope and tradeoffs

This is a **single-workspace local prototype**, not the complete future project
on the resume. The default path has no LLM, neural semantic embeddings, React,
tenant isolation, external ticket provider or production deployment. Handoff is
a UI response, not a staffed helpdesk. Keyword checks are not a general prompt
injection defense; document text cannot execute tools in this implementation.
Keys are shared role credentials, not individual identity or enterprise IAM.
Production needs identity/tenancy, rotation, quotas, migrations, retention and
deployment hardening. Large-document chunking and ANN tuning are future work.

The next meaningful experiment is a learned embedding/model adapter evaluated
against the same questions, not adding more frameworks. No quality, latency or
adoption improvements are claimed without measurements.

Synthetic fixtures only. No PwC, Stillwater or customer material is included.
