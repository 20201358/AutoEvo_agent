"""风险确认节点"""
from core.state import AgentState


def ask_confirmation(state: AgentState) -> AgentState:
    """处理危险命令的确认"""
    # 这个节点会等待用户输入
    # 在实际使用中，这里的逻辑会被 UI 层处理
    
    # 检查用户是否确认
    last_message = ""
    for msg in reversed(state.get("messages", [])):
        if msg.get("role") == "user":
            last_message = msg.get("content", "").strip().lower()
            break
    
    if last_message in ["y", "yes", "确认", "执行"]:
        state["messages"].append({
            "role": "assistant",
            "content": "✅ 已确认执行危险命令"
        })
        state["context"]["confirmed"] = True
    elif last_message in ["n", "no", "取消", "不"]:
        state["messages"].append({
            "role": "assistant",
            "content": "❌ 已取消执行"
        })
        state["context"]["confirmed"] = False
    else:
        # 等待用户确认
        state["next_step"] = "ask_confirmation"  # 保持在当前节点
        
    state["next_step"] = "end"
    return state