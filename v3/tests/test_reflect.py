"""测试技能反思（自进化）链路。

用法：
    python -m v3.tests.test_reflect
"""

from __future__ import annotations

import sys

sys.path.insert(0, "D:/PYTHON/AutoEvo_agent")

from v3.config import settings
from v3.memory import get_conversation_store
from v3.session import AgentSession, delete_conversation
from v3.skillkit import get_skill_library


def banner(text: str) -> None:
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def show(r) -> None:
    print(f"[end_reason={r.end_reason} success={r.task_success}]")
    for msg in r.messages:
        kind = getattr(msg, "type", "?")
        if kind == "ai" and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                print(f"  -> {tc['name']}({str(tc['args'])[:100]})")
        elif kind == "tool":
            print(f"  <- {str(msg.content)[:200]}")
        elif kind == "ai":
            print(f"  [AI]\n{str(msg.content)[:1500]}")
    if r.proposals:
        print(f"[解析出 {len(r.proposals)} 条建议]")
        for p in r.proposals:
            print(f"   - action={p.get('action')} skills={p.get('skills')} reason={p.get('reason')}")
    print(f"[最终答复] {r.final_text[:300]}")


def main() -> int:
    settings.ensure_dirs()
    store = get_conversation_store()
    for c in store.list(limit=200):
        if c.title.startswith("__refl__"):
            delete_conversation(c.id)

    lib = get_skill_library()
    print("技能库:", lib.names())

    banner("场景 A：有复用价值的复杂任务 -> 期待收到创建技能的建议")
    s = AgentSession.new(title="__refl__ 任务A")
    r = s.send(
        f"请帮我把 {settings.DATA_DIR} 目录下所有 .txt 文件的行数统计出来，"
        "并生成一个 report.md 汇总（文件名 + 行数 + 总行数）。"
    )
    show(r)

    banner("场景 B：用户回应建议（如果 A 产出了建议）")
    if r.proposals:
        r2 = s.send("跳过")
        show(r2)
        print(f"[declined={r2.state.get('declined_proposals')}] awaiting={r2.state.get('awaiting_proposal_response')}")
    else:
        print("(A 未产出建议，跳过场景 B)")

    banner("场景 C：一次性琐事 -> 期待 NO_PROPOSAL，保持沉默")
    s2 = AgentSession.new(title="__refl__ 任务C")
    r3 = s2.send("现在几点？用 shell 命令查一下系统时间。")
    show(r3)
    print(f"[proposals={len(r3.proposals)}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
