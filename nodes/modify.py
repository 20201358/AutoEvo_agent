"""修改节点"""
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from core.state import AgentState
from prompts.templates import PromptTemplates  # ✅ 正确导入
from config.settings import settings


def modify_command(state: AgentState) -> AgentState:
    """根据用户反馈修改命令"""
    
    # 初始化修改计数器
    if "modify_count" not in state["context"]:
        state["context"]["modify_count"] = 0
    
    state["context"]["modify_count"] += 1
    
    MAX_MODIFICATIONS = 3
    if state["context"]["modify_count"] > MAX_MODIFICATIONS:
        state["next_step"] = "execute_command"
        state["messages"].append({
            "role": "assistant",
            "content": f"⚠️ 已达到最大修改次数 ({MAX_MODIFICATIONS})，将执行当前命令"
        })
        return state
    
    # 获取用户反馈
    feedback = ""
    for msg in reversed(state.get("messages", [])):
        if msg.get("role") == "user" and msg.get("content") != state["user_request"]:
            feedback = msg["content"]
            break
    
    if not feedback:
        state["next_step"] = "execute_command"
        state["messages"].append({
            "role": "assistant",
            "content": "没有收到修改反馈，将执行当前命令"
        })
        return state
    
    llm = ChatOpenAI(**settings.get_llm_config())
    
    # ✅ 使用 @staticmethod 方法
    prompt = PromptTemplates.modify_command()
    chain = prompt | llm
    
    response = chain.invoke({
        "command": state["command"],
        "feedback": feedback,
        "explanation": state["command_explanation"],
        "modify_count": state["context"]["modify_count"]
    })
    
    new_command = response.content.strip()
    
    # 检查命令是否真的改变了
    if new_command == state["command"]:
        state["next_step"] = "execute_command"
        state["messages"].append({
            "role": "assistant",
            "content": "命令没有变化，准备执行"
        })
        return state
    
    # 更新状态
    old_command = state["command"]
    state["command"] = new_command
    state["command_explanation"] = f"已根据反馈修改 (第{state['context']['modify_count']}次修改)"
    
    # 记录修改历史
    if "modification_history" not in state["context"]:
        state["context"]["modification_history"] = []
    state["context"]["modification_history"].append({
        "old": old_command,
        "new": new_command,
        "feedback": feedback,
        "count": state["context"]["modify_count"]
    })
    
    state["messages"].append({
        "role": "assistant",
        "content": f"🔄 命令已修改: {new_command}"
    })
    
    # 决定下一步
    if state["context"]["modify_count"] >= MAX_MODIFICATIONS:
        state["next_step"] = "execute_command"
        state["messages"].append({
            "role": "assistant",
            "content": f"✅ 已达到修改次数上限，准备执行最终命令"
        })
    else:
        state["next_step"] = "confirm_command"
    
    return state