"""Reproducible synthetic local inference probe; this does not evaluate RAG."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time

from embeddings import EmbeddingError, OllamaEmbedder


def probe(generation_model=None):
    started = time.perf_counter()
    adapter = OllamaEmbedder(timeout=120)
    vectors = adapter.embed(['Projector repair requests require operator approval.',
                             'Room equipment is stored at the synthetic campus desk.'])
    report = {
        'purpose': 'Real local model readiness only; not retrieval or answer-quality evidence',
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'ollama_version': adapter._request('/api/version')['version'],
        'hardware': {'os': platform.system(), 'architecture': platform.machine(), 'logical_cpus': os.cpu_count()},
        'embedding_space': {'id': adapter.space.id, **asdict(adapter.space)},
        'embedding_probe': {'inputs': 2, 'dimensions': [len(v) for v in vectors],
                            'norms': [round(math.sqrt(sum(x*x for x in v)), 6) for v in vectors],
                            'elapsed_seconds_including_setup': round(time.perf_counter() - started, 3)},
    }
    try:
        gpu = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.free', '--format=csv,noheader'],
                             capture_output=True, text=True, timeout=10, check=True)
        report['hardware']['gpu_at_probe'] = gpu.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        report['hardware']['gpu_at_probe'] = 'Not reported; CPU operation is supported'
    if generation_model:
        tags = adapter._request('/api/tags')['models']
        metadata = next((m for m in tags if m['name'] == generation_model), None)
        if not metadata:
            raise EmbeddingError('model_not_installed')
        options = {'temperature': 0, 'seed': 7, 'num_ctx': 2048, 'num_predict': 32}
        result = adapter._request('/api/generate', {
            'model': generation_model, 'prompt': 'Reply with exactly the single word READY.',
            'stream': False, 'think': False, 'keep_alive': 0, 'options': options,
        })
        if not result.get('done') or not result.get('response', '').strip():
            raise EmbeddingError('model_unavailable')
        report['generation_probe'] = {
            'model': generation_model, 'digest': metadata['digest'], 'options': options,
            'prompt': 'Reply with exactly the single word READY.', 'response': result['response'][:200],
            'prompt_eval_count': result.get('prompt_eval_count'), 'eval_count': result.get('eval_count'),
            'total_duration_ns': result.get('total_duration'), 'done': result['done'],
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation-model')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = probe(args.generation_model)
    except EmbeddingError as failure:
        print(json.dumps({'status': 'unavailable', 'error': str(failure)}))
        return 1
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    print(output, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
