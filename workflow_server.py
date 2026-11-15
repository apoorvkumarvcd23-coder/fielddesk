"""Owned demo app subprocess; credentials arrive only through a private config file."""
import argparse
import json
import os
from pathlib import Path
import socket
import threading
import time

import uvicorn
from app import create_app
from embeddings import OllamaEmbedder
from generation import OllamaGenerator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    embedder, generator = OllamaEmbedder(), OllamaGenerator('qwen3.5:9b')
    # Resolve actual model metadata before injecting a real transport failure.
    if 'generation_endpoint' in config:
        generator.base_url = config['generation_endpoint']
    if 'generation_timeout' in config:
        generator.timeout = config['generation_timeout']
    application = create_app(config['database'], config['legacy_user'],
                             config['legacy_operator'], embedder, generator)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        info = {'pid': os.getpid(), 'base_url': 'http://127.0.0.1:' + str(listener.getsockname()[1])}
        descriptor = os.open(config['info'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(info, output)
        server = uvicorn.Server(uvicorn.Config(application, log_level='error', timeout_graceful_shutdown=5))
        def shutdown_when_requested():
            while not Path(config['stop']).exists():
                time.sleep(0.1)
            server.should_exit = True
        threading.Thread(target=shutdown_when_requested,daemon=True).start()
        server.run(sockets=[listener])
        Path(config['exited']).write_text(json.dumps({'pid':os.getpid(),'server_returned':True}),encoding='utf-8')


if __name__ == '__main__':
    main()
