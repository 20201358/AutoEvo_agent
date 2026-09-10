"""Agent 管理模块"""
from typing import Optional, Dict, Any, Generator
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import BaseCheckpointSaver
from core.state import AgentState, create_initial_state
from core.graph import build_agent_graph


class TerminalAgent:
    """终端命令助手 Agent"""
    
    def __init__(
        self,
        checkpointer: Optional[BaseCheckpointSaver] = None,
        thread_id: str = "default"
    ):
        """
        初始化 Agent
        
        Args:
            checkpointer: 状态持久化器，默认使用内存存储
            thread_id: 线程ID，用于区分不同的对话会话
        """
        self.thread_id = thread_id
        self.checkpointer = checkpointer or MemorySaver()
        self._graph = None
        self._compiled_app = None
        self._config = {"configurable": {"thread_id": thread_id}}
        
        # 构建并编译图
        self._build_agent()
    
    def _build_agent(self):
        """构建 Agent 图"""
        workflow = build_agent_graph()
        self._compiled_app = workflow.compile(checkpointer=self.checkpointer)
    
    def invoke(
        self,
        user_input: str,
        state: Optional[AgentState] = None,
        config: Optional[Dict[str, Any]] = None
    ) -> AgentState:
        """
        同步调用 Agent
        
        Args:
            user_input: 用户输入
            state: 当前状态，如果不提供则创建新状态
            config: 配置，覆盖默认配置
            
        Returns:
            更新后的状态
        """
        if state is None:
            state = create_initial_state()
        
        # 更新状态
        state["user_request"] = user_input
        state["messages"].append({
            "role": "user",
            "content": user_input
        })
        
        # 合并配置
        config = {**self._config, **(config or {})}
        
        # 调用 Agent
        result = self._compiled_app.invoke(state, config)
        return result
    
    def stream(
        self,
        user_input: str,
        state: Optional[AgentState] = None,
        config: Optional[Dict[str, Any]] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """
        流式调用 Agent，逐步返回结果
        
        Args:
            user_input: 用户输入
            state: 当前状态
            config: 配置
            
        Yields:
            每一步的状态更新
        """
        if state is None:
            state = create_initial_state()
        
        state["user_request"] = user_input
        state["messages"].append({
            "role": "user",
            "content": user_input
        })
        
        config = {**self._config, **(config or {})}
        
        # 流式执行
        for event in self._compiled_app.stream(state, config):
            yield event
    
    def get_state(self, thread_id: Optional[str] = None) -> Optional[AgentState]:
        """
        获取指定线程的状态
        
        Args:
            thread_id: 线程ID，不提供则使用当前线程
            
        Returns:
            当前状态
        """
        tid = thread_id or self.thread_id
        config = {"configurable": {"thread_id": tid}}
        try:
            state = self._compiled_app.get_state(config)
            return state.values if state else None
        except Exception:
            return None
    
    def update_state(
        self,
        updates: Dict[str, Any],
        thread_id: Optional[str] = None
    ) -> None:
        """
        更新状态
        
        Args:
            updates: 要更新的字段
            thread_id: 线程ID
        """
        tid = thread_id or self.thread_id
        config = {"configurable": {"thread_id": tid}}
        self._compiled_app.update_state(config, updates)
    
    def reset(self, thread_id: Optional[str] = None):
        """
        重置线程状态
        
        Args:
            thread_id: 线程ID
        """
        tid = thread_id or self.thread_id
        # 创建新状态
        new_state = create_initial_state()
        self.update_state(new_state, tid)
    
    def get_history(self, thread_id: Optional[str] = None) -> list:
        """
        获取对话历史
        
        Args:
            thread_id: 线程ID
            
        Returns:
            消息历史列表
        """
        state = self.get_state(thread_id)
        return state.get("messages", []) if state else []
    
    @property
    def graph(self) -> StateGraph:
        """获取图结构"""
        if self._graph is None:
            self._graph = build_agent_graph()
        return self._graph
    
    @property
    def app(self):
        """获取编译后的应用"""
        if self._compiled_app is None:
            self._build_agent()
        return self._compiled_app


# ============ 便捷函数 ============

_agent_instance: Optional[TerminalAgent] = None


def get_agent(
    thread_id: str = "default",
    checkpointer: Optional[BaseCheckpointSaver] = None
) -> TerminalAgent:
    """
    获取或创建 Agent 实例（单例模式）
    
    Args:
        thread_id: 线程ID
        checkpointer: 状态持久化器
        
    Returns:
        Agent 实例
    """
    global _agent_instance
    
    if _agent_instance is None:
        _agent_instance = TerminalAgent(
            checkpointer=checkpointer,
            thread_id=thread_id
        )
    elif thread_id != _agent_instance.thread_id:
        # 如果线程ID不同，创建新实例
        _agent_instance = TerminalAgent(
            checkpointer=checkpointer,
            thread_id=thread_id
        )
    
    return _agent_instance


def reset_agent():
    """重置 Agent 实例"""
    global _agent_instance
    _agent_instance = None


def quick_ask(user_input: str, thread_id: str = "default") -> AgentState:
    """
    快速问答，自动创建 Agent 并执行
    
    Args:
        user_input: 用户输入
        thread_id: 线程ID
        
    Returns:
        执行结果状态
    """
    agent = get_agent(thread_id=thread_id)
    return agent.invoke(user_input)