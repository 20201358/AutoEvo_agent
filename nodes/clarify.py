"""澄清节点"""
from langchain_openai import ChatOpenAI
from core.state import AgentState
from prompts.templates import PromptTemplates  # ✅ 正确导入
from config.settings import settings


def ask_clarification(state: AgentState) -> AgentState:
    """询问用户澄清需求"""
    llm = ChatOpenAI(**settings.get_llm_config(temperature=0.7))
    
    # ✅ 使用 @staticmethod 方法
    prompt = PromptTemplates.clarify_request()
    chain = prompt | llm
    
    response = chain.invoke({
        "request": state["user_request"],
        "command": state.get("command", ""),
        "context": state.get("context", {})
    })
    
    state["messages"].append({
        "role": "assistant",
        "content": f"🤔 {response.content}"
    })
    state["next_step"] = "end"
    
    return state