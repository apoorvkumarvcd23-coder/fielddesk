import pytest

from evaluate_retrieval import evaluate


def test_evaluation_is_repeatable_and_does_not_use_configured_databases(tmp_path, monkeypatch):
    sentinel = tmp_path / 'user.db'
    sentinel.write_bytes(b'private database must remain untouched')
    monkeypatch.setenv('DATABASE_URL', str(sentinel))
    monkeypatch.setenv('TEST_DATABASE_URL', 'postgresql://unreachable/do-not-connect')
    documents = [{'title': 'Equipment', 'body': 'projector repair'}]
    cases = [
        {'id': 'answer', 'query': 'projector repair', 'expected_title': 'Equipment'},
        {'id': 'handoff', 'query': 'I need a human', 'expected_title': None},
    ]
    report = evaluate(documents, cases)
    assert report == evaluate(documents, cases)
    assert report['metrics']['top1_accuracy']['rate'] == 1
    assert report['metrics']['unanswerable_handoff']['rate'] == 1
    assert report['metrics']['grounded_citations'] == {'correct': 1, 'total': 1, 'rate': 1}
    assert sentinel.read_bytes() == b'private database must remain untouched'


def test_evaluation_surfaces_misses_and_rejects_invalid_labels():
    documents = [{'title': 'Equipment', 'body': 'projector repair'}]
    case = {'id': 'missing', 'query': '!!!', 'expected_title': 'Equipment'}
    report = evaluate(documents, [case])
    assert report['metrics']['top1_accuracy']['correct'] == 0
    assert report['results'][0]['routing_correct'] is False
    assert report['metrics']['grounded_citations']['rate'] is None
    with pytest.raises(ValueError, match='Expected title'):
        evaluate(documents, [{**case, 'expected_title': 'Unknown manual'}])
    with pytest.raises(ValueError, match='unique IDs'):
        evaluate(documents, [case, case])
