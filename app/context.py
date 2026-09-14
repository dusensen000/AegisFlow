"""运行时上下文与依赖装配。"""

from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.data.seed import get_logistics, get_orders, get_rules, get_tickets, search_rule_documents
from app.llm.base import LLMClient
from app.llm.client import get_llm_client
from app.services.agent_bus import (
    AgentMessageBus,
    InMemoryAgentMessageBus,
    RedisAgentMessageBus,
)
from app.services.cache import Cache, InMemoryCache, RedisCache
from app.services.memory_store import InMemorySessionStore, RedisSessionStore, SessionStore
from app.services.rag import SimpleRAGRetriever
from app.services.tool_registry import ToolRegistry
from app.tracing import LangfuseTracer, Tracer
from app.services.repository import Repository, DurableSessionStore
from app.services.mcp_client import MCPToolClient
from app.services.tickets import TicketCatalog


@dataclass
class AgentContext:
    settings: Settings
    llm: LLMClient
    tools: ToolRegistry
    rag: SimpleRAGRetriever
    store: SessionStore
    bus: AgentMessageBus
    tracer: Tracer
    graph: object | None = field(default=None)
    repository: object | None = None
    catalog: object | None = None


def create_context(settings: Settings | None = None) -> AgentContext:
    settings = settings or get_settings()
    llm = get_llm_client(settings)
    rules = get_rules()
    orders = get_orders()
    logistics = get_logistics()
    tools = ToolRegistry(orders=orders, logistics=logistics, rules=rules, tickets=get_tickets(),
        rate_limit=settings.tool_rate_limit_per_minute, timeout=settings.tool_timeout_seconds)
    if settings.mcp_url:
        if not settings.mcp_api_key:
            raise RuntimeError("MCP_URL requires MCP_API_KEY")
        tools = MCPToolClient(tools, settings.mcp_url, settings.mcp_api_key)
    if settings.use_redis:
        store: SessionStore = RedisSessionStore(
            redis_url=settings.redis_url,
            ttl_seconds=settings.cache_ttl_seconds,
        )
        bus: AgentMessageBus = RedisAgentMessageBus(redis_url=settings.redis_url)
        cache: Cache = RedisCache(redis_url=settings.redis_url)
    else:
        store = InMemorySessionStore()
        bus = InMemoryAgentMessageBus()
        cache = InMemoryCache()
    rag = SimpleRAGRetriever(
        documents=search_rule_documents(),
        top_k=settings.rag_top_k,
        cache=cache,
    )
    if settings.trace_provider == "langfuse":
        tracer = LangfuseTracer(
            enabled=settings.enable_tracing,
            log_path=settings.trace_log_path,
        )
    else:
        tracer = Tracer(
            enabled=settings.enable_tracing,
            log_path=settings.trace_log_path,
        )
    repository = Repository(settings.database_path)
    store = DurableSessionStore(repository, store if settings.use_redis else None)
    if hasattr(llm, "tracer") or hasattr(llm, "_client"):
        llm.tracer = tracer
    return AgentContext(
        settings=settings,
        llm=llm,
        tools=tools,
        rag=rag,
        store=store,
        bus=bus,
        tracer=tracer,
        repository=repository,
        catalog=TicketCatalog(repository),
    )
