"""LLM 客户端实现：OpenAI 兼容协议的真实模型服务。"""

from __future__ import annotations

import json
import re
import asyncio
from typing import Callable

from pydantic import ValidationError
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.utils import convert_to_openai_messages

from app.config import Settings
from app.llm.base import LLMClient


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    """粗略估算输入文本的 token 数量。"""
    chinese_chars = len(re.findall(r"[一-鿿]", text))
    english_tokens = len(re.findall(r"[a-zA-Z0-9]+", text))
    return chinese_chars + english_tokens


class LLMGenerationError(RuntimeError):
    """真实 LLM 多次重试后仍无法生成合法结果。"""


async def complete_json(
    client: LLMClient,
    system: str,
    user: str,
    temperature: float = 0.2,
) -> dict:
    raw = await client.complete(
        system=system,
        user=user,
        json_mode=True,
        temperature=temperature,
    )
    return json.loads(_strip_code_fence(raw))


async def complete_json_validated(
    client: LLMClient,
    system: str,
    user: str,
    temperature: float = 0.2,
    validator: Callable[[dict], object] | None = None,
    max_attempts: int = 3,
    schema: dict | None = None,
    function_name: str = "submit_result",
) -> dict:
    """调用真实 LLM 生成 JSON；解析或结构校验失败时携带错误反馈自动重试。"""
    feedback = ""
    last_error = "unknown"
    for _ in range(max_attempts):
        current_user = user
        if feedback:
            current_user = (
                f"{user}\n\n你上一次的输出未通过校验，错误信息：\n{feedback}\n"
                "请修正问题，重新严格输出完整 JSON，"
                "不要包含任何解释、前后缀或 Markdown 代码块。"
            )
        try:
            payload = await client.structured(system, current_user, schema, function_name, temperature) if schema else await complete_json(
                client,
                system=system,
                user=current_user,
                temperature=temperature,
            )
            if not isinstance(payload, dict):
                raise ValueError("输出必须是 JSON 对象")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            feedback = last_error
            continue
        if validator is None:
            return payload
        try:
            validator(payload)
            return payload
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
            last_error = str(exc)[:800]
            feedback = last_error
    raise LLMGenerationError(
        f"LLM 连续 {max_attempts} 次未能生成合法 JSON，最后错误：{last_error}"
    )


class OpenAICompatClient(LLMClient):
    """兼容 OpenAI Chat Completions 协议的客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float,
        extra_body: dict | None = None,
        timeout: float = 90,
        max_output_tokens: int = 3072,
    ):
        from openai import AsyncOpenAI

        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.extra_body = extra_body if extra_body is not None else ({"enable_thinking": False} if model.lower().startswith("qwen") else {})
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def complete(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> str:
        request_kwargs = {
            "model": self.model,
            "messages": convert_to_openai_messages([SystemMessage(system), HumanMessage(user)]),
            "temperature": temperature,
        }
        if json_mode:
            request_kwargs["response_format"] = {"type": "json_object"}
        response = await self._chat(**request_kwargs)
        return response.choices[0].message.content or ""

    def _record_usage(self, response):
        if getattr(self, "tracer", None):
            self.tracer.event("llm_usage", model=self.model,
                usage=response.usage.model_dump() if response.usage else None)

    async def _chat(self, **kwargs):
        kwargs.setdefault("max_tokens", self.max_output_tokens)
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        tracer = getattr(self, "tracer", None)
        span = tracer.start_span("llm", model=self.model) if tracer else None
        status = "ok"
        try:
            response = await self._client.chat.completions.create(**kwargs)
            self._record_usage(response)
            return response
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = "failed"
            if tracer:
                tracer.event("llm_error", error=type(exc).__name__)
            raise
        finally:
            if span:
                tracer.end_span(span, status=status)

    async def structured(self, system, user, schema, function_name, temperature=0.2):
        response = await self._chat(
            model=self.model, messages=convert_to_openai_messages([SystemMessage(system), HumanMessage(user)]),
            temperature=temperature,
            tools=[{"type": "function", "function": {"name": function_name, "description": "Return the validated result", "parameters": schema}}],
            tool_choice={"type": "function", "function": {"name": function_name}},
            parallel_tool_calls=False,
        )
        calls = response.choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != function_name:
            raise ValueError("模型未返回指定的结构化函数调用")
        return json.loads(calls[0].function.arguments)

    async def aclose(self):
        await self._client.close()


def get_llm_client(settings: Settings) -> LLMClient:
    if not settings.openai_api_key:
        raise RuntimeError(
            "未配置 OPENAI_API_KEY：请在 .env 中填写真实模型的 API Key 后重试"
        )
    return OpenAICompatClient(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        extra_body=settings.llm_extra_body or None,
        timeout=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
