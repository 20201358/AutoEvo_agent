"""状态定义模块"""
from typing import TypedDict, Annotated, List, Dict, Any, Literal
import operator


class AgentState(TypedDict):
    """AI 助手的状态"""
    messages: Annotated[List[Dict[str, Any]], operator.add]
    user_request: str
    command: str
    command_explanation: str
    is_safe: bool
    risk_level: Literal["low", "medium", "high"]
    next_step: str
    context: Dict[str, Any]  # 包含 modify_count, modification_history 等


def create_initial_state() -> AgentState:
    """创建初始状态"""
    return {
        "messages": [],
        "user_request": "",
        "command": "",
        "command_explanation": "",
        "is_safe": True,
        "risk_level": "low",
        "next_step": "",
        "context": {
            "modify_count": 0,
            "modification_history": [],
            "attempts": 0,
            "max_attempts": 3
        }
    }