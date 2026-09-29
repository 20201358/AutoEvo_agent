"""任务控制工具。

这几个工具与图的状态耦合，由 ``graph.nodes.tools`` 节点特殊处理：

    - ``ask_user``      -> 通过 interrupt 暂停图，把问题抛给用户
    - ``update_plan``   -> 更新 state["plan"]
    - ``finish_task``   -> 结束当前任务
    - ``remember``      -> 写入跨会话的长期记忆文件

它们仍然是合法的 LangChain 工具（LLM 可正常调用），
但直接执行时的实现只提供兜底返回。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ._base import register


# ============================================================
# 交互
# ============================================================

@register(category="task")
def ask_user(question: str, options: str = "", context: str = "") -> str:
    """向用户提问并等待回答（会暂停执行）。

    什么时候用：
      - 需求含糊、有多种合理解法且影响很大时
      - 需要用户提供只有他知道的信息（路径、账号、偏好）
      - 发现任务目标可能已经变化，需要确认是否调整

    什么时候不要用：能自己查清楚的事情不要问；一次只问最关键的问题。

    Args:
        question: 要问的问题，尽量具体。
        options: 可选的候选答案，用 "|" 分隔，例如 "是|否|先跳过"。留空表示自由回答。
        context: 为什么要问（帮助用户理解背景）。
    """
    opts = [o.strip() for o in (options or "").split("|") if o.strip()]
    if opts:
        return f"(等待用户回答) {question}  候选: {', '.join(opts)}"
    return f"(等待用户回答) {question}"


# ============================================================
# 计划
# ============================================================

@register(category="task")
def update_plan(steps: str, reason: str = "") -> str:
    """更新当前任务的执行计划。

    当你发现原计划不再适用（用户改了需求、发现了新情况、某步走不通）时调用。
    传入完整的步骤列表，而不是增量。

    Args:
        steps: 完整步骤列表，每行一步，用换行分隔。例如：
            "1. 读取 config.yaml\n2. 修改数据库连接地址\n3. 重启服务并验证"
        reason: 为什么调整计划。
    """
    lines = [ln.strip() for ln in (steps or "").splitlines() if ln.strip()]
    return f"(计划已更新为 {len(lines)} 步) 原因: {reason or '(未说明)'}"


# ============================================================
# 结束
# ============================================================

@register(category="task")
def finish_task(summary: str, success: bool = True) -> str:
    """声明当前任务已完成（或已无法继续），并给出总结。

    调用它之后本回合就会结束，然后进入技能反思阶段。

    Args:
        summary: 给用户的最终答复，要说清做了什么、结果如何、有什么遗留问题。
        success: 任务是否达成目标。
    """
    return f"(任务结束: {'成功' if success else '未完成'}) {summary}"


# ============================================================
# 长期记忆
# ============================================================

def memory_file() -> Path:
    """返回长期记忆文件路径。"""
    return Path(settings.DATA_DIR) / "MEMORY.md"


@register(category="task", risk="low")
def remember(note: str, category: str = "general") -> str:
    """把一条值得长期保留的事实写入跨会话记忆（用户偏好、项目约定、环境特点）。

    只记稳定的、以后还用得上的信息；不要记临时状态或一次性结果。

    Args:
        note: 要记住的内容，一句话说清。
        category: 分类标签，如 user_preference / project_convention / environment。
    """
    path = memory_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- [{stamp}] ({category}) {note.strip()}\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        return f"错误：写入记忆失败 -> {e}"
    return f"已记住：{note.strip()}"


@register(category="task")
def recall_memory() -> str:
    """读取跨会话长期记忆（用户偏好、项目约定等）。"""
    path = memory_file()
    if not path.exists():
        return "(暂无长期记忆)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        return f"错误：读取记忆失败 -> {e}"
    return text or "(暂无长期记忆)"


def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


TASK_TOOLS = [
    "ask_user",
    "update_plan",
    "finish_task",
    "remember",
    "recall_memory",
]

__all__ = [
    "ask_user",
    "update_plan",
    "finish_task",
    "remember",
    "recall_memory",
    "memory_file",
    "TASK_TOOLS",
]