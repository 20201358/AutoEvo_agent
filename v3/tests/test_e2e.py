"""端到端冒烟测试：跑一轮真实的 LLM 交互。

用法：
    python -m v3.tests.test_e2e
"""

from __future__ import annotations

import sys

sys.path.insert(0, "D:/PYTHON/AutoEvo_agent")

from v3.config import settings
from v3.memory import get_conversation_store
from v3.session import AgentSession, delete_conversation


def banner(text: str) -> None:
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def show(result) -> None:
    print(f"[end_reason={result.end_reason} success={result.task_success}]")
    if result.interrupt:
        print(f"[INTERRUPT] {result.interrupt}")
    for msg in result.messages:
        kind = getattr(msg, "type", "?")
        if kind == "ai" and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                print(f"  -> 调用 {tc['name']}({str(tc['args'])[:120]})")
        elif kind == "tool":
            body = str(msg.content)[:300].replace("\n", " | ")
            print(f"  <- 工具结果: {body}")
        elif kind == "ai":
            print(f"  [AI] {str(msg.content)[:600]}")
    if result.plan:
        print("[计划]")
        for i, s in enumerate(result.plan, 1):
            print(f"   {i}. [{s.get('status')}] {s.get('description')}")
    if result.proposals:
        print("[技能建议]")
        for p in result.proposals:
            print(f"   - {p.get('action')} {p.get('skills')} :: {p.get('reason')}")
    print(f"[最终答复]\n{result.final_text}")


def main() -> int:
    settings.ensure_dirs()
    store = get_conversation_store()
    # 清理旧测试会话
    for c in store.list(limit=200):
        if c.title.startswith("__e2e__"):
            delete_conversation(c.id)

    banner("场景 1：纯问答（不需要工具）")
    s = AgentSession.new(title="__e2e__ 问答")
    r = s.send("用一句话说明你有哪些工具可以用来操作我的电脑。")
    show(r)

    banner("场景 2：写文件 + 读回验证")
    r = s.send(
        f"请在 {settings.DATA_DIR} 下创建 hello_v3.txt，内容为三行："
        "第一行 Hello、第二行 AutoEvo、第三行 v3。然后读回来确认。"
    )
    show(r)

    banner("场景 3：shell 命令 + 持久化会话")
    r = s.send("把工作目录切到项目根目录 D:/PYTHON/AutoEvo_agent，然后列出里面的目录。")
    show(r)

    return 0


if __name__ == "__main__":
    sys.exit(main())
