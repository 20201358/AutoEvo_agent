"""执行节点"""
from core.state import AgentState
from tools.command_executor import CommandExecutor
from tools.security_checker import SecurityChecker


def execute_command(state: AgentState) -> AgentState:
    """准备执行命令"""
    command = state["command"]
    
    # 安全检查
    safety_check = SecurityChecker.check_command_safety(command)
    
    # 添加到消息
    state["messages"].append({
        "role": "assistant",
        "content": f"📝 命令: {command}\n\n📖 说明: {state['command_explanation']}"
    })
    
    # 如果有警告
    if safety_check.get("warnings"):
        warnings = "\n".join([f"⚠️ {w}" for w in safety_check["warnings"]])
        state["messages"].append({
            "role": "assistant",
            "content": f"安全警告:\n{warnings}"
        })
    
    # 检查是否需要确认
    if not state["is_safe"] or safety_check.get("risk_level") == "high":
        state["next_step"] = "ask_confirmation"
        state["messages"].append({
            "role": "assistant",
            "content": "⚠️ 这是一个高风险命令，请回复 'y' 确认执行，或 'n' 取消"
        })
    else:
        state["next_step"] = "end"
        state["messages"].append({
            "role": "assistant",
            "content": "✅ 命令已准备就绪，可以执行"
        })
    
    # 保存安全检查结果到上下文
    state["context"]["safety_check"] = safety_check
    
    return state