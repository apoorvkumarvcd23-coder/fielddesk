import uvicorn
from app import create_app
from settings import load_env

load_env()
import os
from embeddings import EmbeddingError, OllamaEmbedder
from generation import OllamaGenerator

embedder = generator = None
if os.getenv('FIELDDESK_RAG') == 'ollama':
    try:
        embedder = OllamaEmbedder()
        generator = OllamaGenerator(os.getenv('FIELDDESK_GENERATION_MODEL', 'qwen3.5:9b'))
    except EmbeddingError:
        print('Local model setup unavailable: RAG will hand off. Check models and restart.')
uvicorn.run(create_app(rag_embedder=embedder, rag_generator=generator), host='127.0.0.1', port=8101)
