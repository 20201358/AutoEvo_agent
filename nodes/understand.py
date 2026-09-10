"""理解请求节点"""
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from core.state import AgentState
from prompts.templates import PromptTemplates  # ✅ 正确导入
from config.settings import settings
import json


def understand_request(state: AgentState) -> AgentState:
    """理解用户的自然语言请求"""
    llm = ChatOpenAI(**settings.get_llm_config())
    
    # ✅ 使用 @staticmethod 方法
    prompt = PromptTemplates.understand_request()
    chain = prompt | llm
    
    response = chain.invoke({"request": state["user_request"]})
    
    try:
        # 提取 JSON 内容
        content = response.content
        # 处理可能的 Markdown 代码块
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        
        result = json.loads(content.strip())
        state["command"] = result.get("command", "")
        state["command_explanation"] = result.get("explanation", "")
        state["risk_level"] = result.get("risk_level", "low")
        state["is_safe"] = state["risk_level"] != "high"
        
    except json.JSONDecodeError as e:
        # 如果解析失败，尝试直接使用响应
        state["command"] = response.content
        state["command_explanation"] = "命令已生成"
        state["risk_level"] = "medium"
        state["is_safe"] = False
    
    state["next_step"] = "confirm_command"
    return state