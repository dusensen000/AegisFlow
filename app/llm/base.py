"""LLM 抽象接口。"""

from abc import ABC, abstractmethod
import json


class LLMClient(ABC):
    model: str

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> str:
        """执行一次对话补全，返回文本内容。"""

    async def structured(self, system: str, user: str, schema: dict,
                         function_name: str, temperature: float = 0.2) -> dict:
        # Test doubles can implement complete; production overrides this with function calling.
        return json.loads(await self.complete(system, user, json_mode=True, temperature=temperature))
