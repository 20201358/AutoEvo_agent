"""确认节点"""
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from core.state import AgentState
from prompts.templates import PromptTemplates  # ✅ 正确导入
from config.settings import settings


def confirm_command(state: AgentState) -> AgentState:
    """确认命令是否准确"""
    
    # 检查修改次数
    modify_count = state["context"].get("modify_count", 0)
    
    # 如果已经修改过多次，直接执行
    if modify_count >= 2:
        state["next_step"] = "execute_command"
        state["messages"].append({
            "role": "assistant",
            "content": f"已修改 {modify_count} 次，将执行当前命令"
        })
        return state
    
    llm = ChatOpenAI(**settings.get_llm_config(temperature=0.1))
    
    # ✅ 使用 @staticmethod 方法
    prompt = PromptTemplates.confirm_command()
    chain = prompt | llm
    
    # 准备历史消息
    messages = state.get("messages", [])[-5:]
    messages_str = "\n".join([
        f"{msg.get('role', 'unknown')}: {msg.get('content', '')}"
        for msg in messages
    ])
    
    response = chain.invoke({
        "request": state["user_request"],
        "command": state["command"],
        "messages": messages_str,
        "risk_level": state.get("risk_level", "low"),
        "modify_count": modify_count
    })
    
    decision = response.content.strip().lower()
    
    # 决策逻辑
    if "execute" in decision:
        state["next_step"] = "execute_command"
    elif "modify" in decision:
        if modify_count >= 2:
            state["next_step"] = "execute_command"
            state["messages"].append({
                "role": "assistant",
                "content": "已达到修改次数上限，执行当前命令"
            })
        else:
            state["next_step"] = "modify_command"
    else:
        state["next_step"] = "ask_clarification"
    
    return state