"""LLM 工厂。

统一用 OpenAI 兼容接口（智谱 GLM / OpenAI / 本地兼容服务均可）。
规划与反思这类结构化任务用低温度，对话用配置温度。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from ..config import settings

log = logging.getLogger(__name__)


# ============================================================
# 构造
# ============================================================

def get_llm(temperature: Optional[float] = None) -> ChatOpenAI:
    """创建一个 ChatOpenAI 客户端。"""
    return ChatOpenAI(**settings.llm_config(temperature=temperature))


def get_planner_llm() -> ChatOpenAI:
    """规划 / 反思用的低温度模型。"""
    return get_llm(temperature=settings.PLANNER_TEMPERATURE)


def bind_tools(llm: BaseChatModel, tools: Sequence[BaseTool]) -> BaseChatModel:
    """绑定工具（tools 为空时原样返回）。"""
    if not tools:
        return llm
    return llm.bind_tools(list(tools))


# ============================================================
# JSON 解析
# ============================================================

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """从模型输出里尽力抽出 JSON。

    依次尝试：
      1. 直接解析
      2. ```json ... ``` 代码块
      3. 第一个 { 到最后一个 }、或第一个 [ 到最后一个 ] 的子串
    """
    if not text:
        return None
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except Exception:
                continue
    return None


def parse_steps(text: str, max_steps: int = 12) -> list[str]:
    """从模型输出解析出步骤列表，尽量宽容。"""
    data = extract_json(text)

    steps: list[str] = []

    if isinstance(data, dict):
        raw = data.get("steps") or data.get("plan") or []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    steps.append(item.strip())
                elif isinstance(item, dict):
                    desc = item.get("description") or item.get("step") or item.get("title")
                    if desc:
                        steps.append(str(desc).strip())
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                steps.append(item.strip())
            elif isinstance(item, dict):
                desc = item.get("description") or item.get("step") or item.get("title")
                if desc:
                    steps.append(str(desc).strip())

    if not steps:
        # 兜底：按行解析 "1. xxx" / "- xxx"
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^(?:[-*•]|\d+[.、)])\s*", "", line).strip()
            if len(line) > 3:
                steps.append(line)

    # 去掉明显的元信息行
    steps = [s for s in steps if s and not s.startswith(("```", "{", "["))]

    return steps[:max_steps]


__all__ = [
    "get_llm",
    "get_planner_llm",
    "bind_tools",
    "extract_json",
    "parse_steps",
]
