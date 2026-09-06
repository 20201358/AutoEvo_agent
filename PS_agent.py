from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

#point state
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]  # 自动合并消息
    user_info: str
    current_step: int


def workNode(state: AgentState)->dict:
    
    messages=state['messages']

    result="处理结果"

    return {"messages": [{"role": "ai", "content": result}]}