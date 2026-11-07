# Offline retrieval evaluation

Run after installing `requirements-dev.txt`:

```sh
python evaluate_retrieval.py
```

The runner ingests the three synthetic manuals into a new temporary SQLite
database, sends all 16 labelled questions through the real `/chat` API and prints
JSON with aggregate metrics and every query's result. It does not use `.env`,
`DATABASE_URL`, `TEST_DATABASE_URL`, the demo database, a model API or network
requests. It deletes its temporary database when the run finishes.

Use `python evaluate_retrieval.py --fail-on-miss` for a strict experiment: it exits
1 if any answerable question misses its expected first citation, routing is
wrong or a citation cannot be matched to the ingested source. The default command
exits 0 after a completed evaluation even when quality misses are reported.
Runtime/API errors still fail the command. The current baseline intentionally
has misses, so strict mode is not a passing CI gate.

## Baseline measured on 7 November 2026

| Metric | Result | Definition |
| --- | --- | --- |
| Top-1 accuracy | 9/10 | Answerable questions whose first citation is the expected manual |
| Hit at 3 | 9/10 | Answerable questions with the expected manual anywhere in the returned citations |
| Unanswerable handoff | 4/6 | Questions labelled for handoff that actually receive one |
| Routing accuracy | 13/16 | Questions answered or handed off according to their label |
| Grounded citations | 18/18 | Returned citations whose ID, title and nonempty quote match an ingested manual |

The full output is in `retrieval-baseline.json`. Grounded quotations do not prove
that a question was answered or that the source is relevant. In particular:

- The paraphrase about a laptop being unable to get online over wireless is
  handed off despite the network manual being the intended source.
- Asking the price of a projector repair retrieves a repair manual that has no
  price. This should hand off according to the evaluation label.
- Asking for a WiFi password returns notes rather than handing off. No password
  is disclosed; the notes do not contain one. The routing decision is still wrong.

These cases expose limitations in lexical similarity and the fixed score
threshold. They are candidates for an evaluated embedding/answerability adapter,
not evidence that raising or lowering one threshold will solve all cases.

## Scope and reproducibility

The labels in `fixtures/retrieval_cases.json` were written against the current
three demo manuals. Ten cases are answerable; six require handoff, including
two explicit escalation/instruction cases. Both direct lexical queries and
paraphrases are included. The same fixture is reusable for future comparisons.
Results are deterministic for the current SQLite hash-vector path. A zero-sized
metric denominator is represented as `null`, not a perfect score.

This is a small development diagnostic, not an independent benchmark, a held-out
test of generalisation or a measurement of production users. New model adapters
should also be checked against a larger independently labelled set. PostgreSQL
adapter correctness remains covered separately by the existing CI suite; these
numbers describe only the default SQLite retrieval path.
