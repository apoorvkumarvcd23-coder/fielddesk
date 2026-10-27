import uvicorn
from app import create_app
from settings import load_env

load_env()
uvicorn.run(create_app(), host='127.0.0.1', port=8101)
