import os


class Settings:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@postgres:5432/transactions",
    )
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    upload_dir = os.getenv("UPLOAD_DIR", "/app/uploads")
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model = os.getenv("OPENROUTER_MODEL", "openrouter/free")
    openrouter_base_url = os.getenv(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    )


settings = Settings()
