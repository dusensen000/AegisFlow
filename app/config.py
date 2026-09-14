"""应用配置。"""

from functools import lru_cache
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AegisFlow"
    debug: bool = False

    # LLM 配置：兼容 OpenAI 协议的真实模型服务
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    llm_model: str = "qwen-plus"
    llm_temperature: float = 0.2
    llm_extra_body: dict = Field(default_factory=dict)
    llm_timeout_seconds: float = Field(default=90, gt=0, le=180)
    llm_max_output_tokens: int = Field(default=3072, ge=512, le=16384)

    # 会话与缓存
    use_redis: bool = False
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 3600
    rate_limit_per_minute: int = 60
    database_path: str = "data/aegisflow.db"
    demo_auth: bool = True
    cookie_secure: bool = False
    api_users: dict[str, dict] = Field(default_factory=dict)
    allowed_origins: list[str] = Field(default_factory=list)
    tool_timeout_seconds: float = Field(default=15, gt=0)
    tool_rate_limit_per_minute: int = Field(default=120, ge=1)
    mcp_url: str = ""
    mcp_api_key: str = ""

    # Agent 编排
    max_steps: int = Field(default=24, ge=1)
    max_replans: int = Field(default=2, ge=0, le=2)
    memory_max_facts: int = Field(default=8, ge=1)
    memory_context_tokens: int = Field(default=3000, ge=256)
    rag_top_k: int = 5

    # 可观测性
    enable_tracing: bool = True
    trace_provider: str = "json"  # json | langfuse
    trace_log_path: str = "logs/traces.jsonl"

    # 评测
    eval_top_k: int = 5
    eval_report_path: str = "reports/eval_report.json"
    judge_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
