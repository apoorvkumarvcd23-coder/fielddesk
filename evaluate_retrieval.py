"""Offline evaluation of the actual chat API using a disposable SQLite database."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app import create_app

ROOT = Path(__file__).resolve().parent
USER = {'Authorization': 'Bearer evaluation-user'}
OPERATOR = {'Authorization': 'Bearer evaluation-operator'}


def evaluate(documents, cases):
    """Return deterministic counts and per-query failures, never touching user data."""
    titles = {doc['title'] for doc in documents}
    if len(titles) != len(documents):
        raise ValueError('Evaluation documents must have unique titles')
    ids = [case['id'] for case in cases]
    if len(ids) != len(set(ids)) or not cases:
        raise ValueError('Evaluation cases must be nonempty and have unique IDs')
    if any(case['expected_title'] is not None and case['expected_title'] not in titles for case in cases):
        raise ValueError('Expected title is absent from evaluation documents')

    results = []
    citation_count = grounded_count = 0
    with TemporaryDirectory(prefix='fielddesk-evaluation-') as directory:
        # Explicit path and keys override DATABASE_URL and any local credentials.
        app = create_app(str(Path(directory) / 'evaluation.db'), 'evaluation-user', 'evaluation-operator')
        with TestClient(app) as client:
            sources = {}
            for document in documents:
                response = client.post('/documents', headers=OPERATOR, json=document)
                response.raise_for_status()
                sources[response.json()['id']] = document
            for case in cases:
                response = client.post('/chat', headers=USER, json={'message': case['query']})
                response.raise_for_status()
                answer = response.json()
                returned = [citation['title'] for citation in answer['citations']]
                for citation in answer['citations']:
                    citation_count += 1
                    source = sources.get(citation['id'])
                    grounded_count += int(bool(source and citation['title'] == source['title']
                                               and citation['quote'] and citation['quote'] in source['body']))
                expected = case['expected_title']
                results.append({
                    'id': case['id'], 'query': case['query'], 'expected_title': expected,
                    'returned_titles': returned, 'handoff': answer['handoff'],
                    'top1_correct': bool(returned and returned[0] == expected) if expected else None,
                    'hit_at_3': expected in returned if expected else None,
                    'routing_correct': answer['handoff'] == (expected is None),
                })

    answerable = [row for row in results if row['expected_title'] is not None]
    unanswerable = [row for row in results if row['expected_title'] is None]

    def metric(correct, total):
        return {'correct': correct, 'total': total, 'rate': correct / total if total else None}

    return {
        'mode': 'offline-extractive', 'storage': 'disposable-sqlite',
        'documents': len(documents), 'queries': len(results),
        'metrics': {
            'top1_accuracy': metric(sum(row['top1_correct'] for row in answerable), len(answerable)),
            'hit_at_3': metric(sum(row['hit_at_3'] for row in answerable), len(answerable)),
            'unanswerable_handoff': metric(sum(row['routing_correct'] for row in unanswerable), len(unanswerable)),
            'routing_accuracy': metric(sum(row['routing_correct'] for row in results), len(results)),
            'grounded_citations': metric(grounded_count, citation_count),
        },
        'results': results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fail-on-miss', action='store_true', help='Exit 1 for retrieval, routing or citation misses')
    args = parser.parse_args()
    documents = json.loads((ROOT / 'fixtures/manuals.json').read_text(encoding='utf-8'))
    cases = json.loads((ROOT / 'fixtures/retrieval_cases.json').read_text(encoding='utf-8'))
    report = evaluate(documents, cases)
    print(json.dumps(report, indent=2))
    missed = any(row['top1_correct'] is False or not row['routing_correct'] for row in report['results'])
    citations = report['metrics']['grounded_citations']
    missed = missed or citations['correct'] != citations['total']
    return int(args.fail_on_miss and missed)


if __name__ == '__main__':
    raise SystemExit(main())
