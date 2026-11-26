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
    db_path: str = os.getenv("DB_PATH", "data/app.db")
    chroma_dir: str = os.getenv("CHROMA_DIR", "data/chroma")


settings = Settings()
