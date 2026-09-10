"""图结构定义"""
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from core.state import AgentState
from nodes.understand import understand_request
from nodes.confirm import confirm_command
from nodes.execute import execute_command
from nodes.modify import modify_command
from nodes.clarify import ask_clarification
from nodes.risk_confirm import ask_confirmation


def build_agent_graph() -> StateGraph:
    """构建 Agent 图"""
    
    # 创建状态图
    workflow = StateGraph(AgentState)
    
    # 添加所有节点
    workflow.add_node("understand", understand_request)
    workflow.add_node("confirm", confirm_command)
    workflow.add_node("execute", execute_command)
    workflow.add_node("modify", modify_command)
    workflow.add_node("clarify", ask_clarification)
    workflow.add_node("risk_confirm", ask_confirmation)
    
    # 设置入口
    workflow.set_entry_point("understand")
    
    # 理解 -> 确认
    workflow.add_conditional_edges(
        "understand",
        lambda state: state["next_step"],
        {
            "confirm_command": "confirm",
        }
    )
    
    # 确认 -> 执行/修改/澄清
    workflow.add_conditional_edges(
        "confirm",
        lambda state: state["next_step"],
        {
            "execute_command": "execute",
            "modify_command": "modify",
            "ask_clarification": "clarify",
        }
    )
    
    # 执行 -> 风险确认/结束
    workflow.add_conditional_edges(
        "execute",
        lambda state: state["next_step"],
        {
            "ask_confirmation": "risk_confirm",
            "end": END,
        }
    )
    
    # 修改 -> 确认（重新进入确认流程）
    workflow.add_edge("modify", "confirm")
    
    # 结束节点
    workflow.add_edge("clarify", END)
    workflow.add_edge("risk_confirm", END)
    
    return workflow


def create_agent():
    """创建并编译 Agent"""
    workflow = build_agent_graph()
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    return app