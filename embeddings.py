"""Separate deterministic baseline and bounded local Ollama embedding adapters."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHUNKING_VERSION = 'chars-600-overlap-80-v1'


def embedding(text):
    """Signed hashed bag-of-words baseline, NOT learned semantic embeddings."""
    values = [0.0] * 256
    for word in re.findall(r'[a-z0-9]+', text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        values[digest[0]] += 1 if digest[1] % 2 else -1
    norm = math.sqrt(sum(x * x for x in values)) or 1
    return [x / norm for x in values]


class EmbeddingError(RuntimeError):
    """Safe category only: do not persist response bodies, URLs or source text."""


@dataclass(frozen=True)
class Space:
    backend: str
    model: str
    digest: str
    dimensions: int
    chunking_version: str = CHUNKING_VERSION

    @property
    def id(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class HashEmbedder:
    space = Space('hash', 'signed-bow-sha256-v1', 'algorithm-v1', 256)

    def embed(self, texts):
        return [embedding(text) for text in texts]

    def embed_query(self, text):
        return embedding(text)


def validate_vectors(vectors, expected_count, dimensions):
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise EmbeddingError('invalid_embedding')
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
            raise EmbeddingError('invalid_embedding')


class OllamaEmbedder:
    def __init__(self, model='nomic-embed-text:v1.5', base_url='http://127.0.0.1:11434', timeout=30):
        parsed = urlparse(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.username or parsed.password:
            raise ValueError('Only a local loopback Ollama endpoint is supported')
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        digest = self._digest()
        vectors = self._embed(['search_document: Synthetic model readiness probe.'])
        if not isinstance(vectors, list) or len(vectors) != 1 or not isinstance(vectors[0], list):
            raise EmbeddingError('invalid_embedding')
        dimensions = len(vectors[0])
        if not 1 <= dimensions <= 4096:
            raise EmbeddingError('invalid_embedding')
        validate_vectors(vectors, 1, dimensions)
        self.space = Space('ollama', model, digest, dimensions)
        if self._digest() != digest:
            raise EmbeddingError('model_changed')

    def _request(self, path, payload=None):
        request = Request(self.base_url + path, data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise EmbeddingError('invalid_embedding')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise EmbeddingError('invalid_embedding')
                return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise EmbeddingError('model_unavailable') from None

    def _digest(self):
        data = self._request('/api/tags')
        for model in data.get('models', []):
            if model.get('name') == self.model and isinstance(model.get('digest'), str) and model['digest']:
                return model['digest']
        raise EmbeddingError('model_not_installed')

    def _embed(self, texts):
        data = self._request('/api/embed', {'model': self.model, 'input': texts, 'truncate': False, 'keep_alive': '2m'})
        return data.get('embeddings')

    def embed(self, texts):
        if not 1 <= len(texts) <= 8 or any(len(text) > 600 for text in texts):
            raise ValueError('Embedding batches require 1–8 chunks of at most 600 characters')
        if self._digest() != self.space.digest:
            raise EmbeddingError('model_changed')
        vectors = self._embed(['search_document: ' + text for text in texts])
        validate_vectors(vectors, len(texts), self.space.dimensions)
        if self._digest() != self.space.digest:
            raise EmbeddingError('model_changed')
        return vectors

    def embed_query(self, text):
        if not text.strip() or len(text) > 2000:
            raise ValueError('Query requires 1–2000 characters')
        if self._digest() != self.space.digest:
            raise EmbeddingError('model_changed')
        vectors = self._embed(['search_query: ' + text])
        validate_vectors(vectors, 1, self.space.dimensions)
        if self._digest() != self.space.digest:
            raise EmbeddingError('model_changed')
        return vectors[0]
