from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "ride_hailing"
    db_user: str = "root"
    db_password: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.05

    # NL2SQL 执行护栏：结果行数上限 + 单条查询超时（秒）
    sql_max_rows: int = 200
    sql_timeout_seconds: float = 5.0

    debug: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
