import os


class Settings:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@postgres:5432/transactions",
    )
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    upload_dir = os.getenv("UPLOAD_DIR", "/app/uploads")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")


settings = Settings()
