import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("LLM_MODEL", "qwen3.5:4b")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "ollama")
    app_api_key: str = os.getenv("APP_API_KEY", "change-me")
    embed_model: str = os.getenv("EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{os.getenv('DB_PATH', 'data/app.db')}")
    vector_backend: str = os.getenv("VECTOR_BACKEND", "auto")
    chroma_dir: str = os.getenv("CHROMA_DIR", "data/chroma")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    task_always_eager: bool = os.getenv("TASK_ALWAYS_EAGER", "false").lower() == "true"


settings = Settings()
