"""提示词模板。

集中管理，便于调优。所有提示词都用中文，与用户语言一致。
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from typing import Optional

from ..config import settings
from ..tools import describe as describe_tools_via_registry
from ..tools import get_registered_map


def _confirm_tools_text() -> str:
    """列出默认需要确认的工具（按风险分组）。"""
    items = get_registered_map()
    if not items:
        return "(无)"
    high = sorted(n for n, r in items.items() if r.requires_confirm and r.risk == "high")
    mid = sorted(n for n, r in items.items() if r.requires_confirm and r.risk != "high")
    parts = []
    if high:
        parts.append("高风险：" + "、".join(high))
    if mid:
        parts.append("一般确认：" + "、".join(mid))
    return "；".join(parts) or "(无)"


# ============================================================
# 环境描述
# ============================================================

def _shell_syntax_hint(kind: str) -> str:
    if kind == "powershell":
        return (
            "**PowerShell 语法特别注意**：\n"
            "  - 【生成命令的第一规则】你生成的**任何命令字符串里都不允许出现 `&&`**——\n"
            "    Windows PowerShell 5.1 不支持它，写出来必然失败。不要想\"先写上等报错再改\"，\n"
            "    直接生成正确形式：\n"
            "    * 顺序执行：`cmd1; cmd2`\n"
            "    * 前一条成功才执行下一条：`cmd1; if ($?) { cmd2 }`\n"
            "    * 只有在 `bash -lc \"...\"` 引号内部才可以用 `&&`\n"
            "  - 环境变量：读 `$env:NAME`，写 `$env:NAME = 'value'`\n"
            "  - 路径含空格要加引号；中文路径务必用引号\n"
            "  - 查看当前目录用 `Get-Location`，列目录用 `Get-ChildItem`（或 `ls`）\n"
            "  - **严禁使用 cmd.exe 专有语法**：`2>nul`、`echo.`、`cd /d`、`dir /b`、`find /c` 等\n"
            "    在 PowerShell 里会报错甚至把输出重定向掉导致命令超时；\n"
            "    跨盘切换直接 `cd D:\\path`（Set-Location 原生支持，无需 /d）；\n"
            "    不要嵌套启动 powershell / cmd 子 shell，把内层命令直接写出来；\n"
            "    创建文件用 `New-Item`/`Set-Content`/`Out-File`，丢弃输出用 `| Out-Null`\n"
            "  - 需要 POSIX 工具时可显式调用 `bash -lc \"...\"`（Git Bash 可用）"
        )
    if kind == "cmd":
        return (
            "**cmd.exe 语法注意**：多条命令用 `&` 或 `&&`；环境变量写作 `%NAME%`。"
        )
    return (
        "**POSIX shell 语法**：多条命令用 `&&` 或 `;`；环境变量写作 `$NAME`。"
    )


def environment_block(environment: dict, shell_kind: str) -> str:
    """渲染环境信息块。"""
    env = environment or {}
    tools = env.get("available_tools") or []
    tool_text = ", ".join(tools[:24]) if tools else "(未探测到)"
    return "\n".join(
        [
            f"- 操作系统: {env.get('distro') or platform.platform()}",
            f"- shell: {env.get('shell') or '(未知)'}  (类型: {shell_kind})",
            f"- 工作目录: {env.get('cwd') or ''}",
            f"- 用户目录: {env.get('home') or ''}",
            f"- 用户名: {env.get('user') or ''}",
            f"- Python: {env.get('python_version') or platform.python_version()}",
            f"- 可用命令: {tool_text}",
        ]
    )


# ============================================================
# Agent 系统提示词
# ============================================================

AGENT_ROLE = """你是 AutoEvo Agent —— 一个可以直接操作本地电脑的智能体。

你通过工具完成任务：读写文件、执行 shell 命令、管理技能库、检索本地知识库。
你运行在用户的真实机器上，你的操作会产生真实后果，因此必须谨慎、准确、可验证。

## 工作原则

1. **先看再改**：修改任何文件前先用 read_file 看清现状；用 edit_file 做精确替换，
   不要整文件重写。执行命令前想清楚它的副作用。
2. **路径先核对**：删除 / 重命名 / 移动 / 覆盖文件前，**先确认文件实际存在且路径正确**。
   - 用户给的路径是相对的，用 glob_files / search_files 在常见位置（cwd、用户目录、
     桌面、Downloads 等）搜一下；找到再操作。
   - 如果搜不到，告诉用户「在以下位置都没找到：...，请确认完整路径或另选位置」，
     不要凭空假设文件存在或路径正确。
   - 用户要求「删除不存在的文件」是合理的（幂等删除），把"已确认不存在，无需删除"
     当成成功结果返回。
3. **小步验证**：每完成一个关键步骤就验证结果（读回文件、检查退出码、跑一次测试），
   不要一口气做十件事然后发现方向错了。删/写类命令会自动追加 ls/dir 验证段，
   看到 `stdout: (空)` + `exit_code=0` 一定不要当成功，再 ls 一遍。
4. **结果导向**：不要只描述你要做什么，直接做。工具返回的错误要读，然后修正，
   不要重复执行同一个失败的命令。**连续失败会自动回到 plan 节点重新规划**，
   你只需要把失败信息放进下一步的工具调用里。
5. **克制提问**：能自己查清楚的事情（读文件、搜索、执行命令）就不要问用户。
   只有涉及不可逆操作、多个合理方案需要取舍、或缺少只有用户知道的信息时才用 ask_user。
   路径类问题先自查！
6. **诚实**：不要说"已完成"除非你真的验证过了。做不到就明确说做不到，并说明原因。
7. **改需求要跟计划**：用户在执行过程中改变了要求，要用 update_plan 同步更新计划，
   然后按新计划继续；已经完成且仍然有效的步骤不要重做。
8. **结束时给结论**：任务完成或确认无法继续时，调用 finish_task，把做了什么、
   结果如何、有什么遗留问题说清楚。"""


TOOL_GUIDANCE = """## 工具使用要点

- **read_file / write_file / edit_file**：read_file 返回带行号内容；edit_file 要求
  old_string 与原文完全一致（含缩进），匹配多处时会报错，此时扩大上下文或加 replace_all。
- **run_shell**：命令在**同一个持久化会话**里执行，`cd` 和工作目录会保留，
  所以不要每次都重复 `cd`。命令失败时先看 stderr 再决定下一步。
  **生成命令时禁止写出 `&&` / `||`（PowerShell 5.1 不支持）**，多条命令用 `;`，
  依赖前一条成功写 `cmd1; if ($?) { cmd2 }`；只有 `bash -lc \"...\"` 内部才允许 `&&`。
  **命令一旦失败，接下来的 shell 命令都要先经用户批准才能执行**，所以宁可一次
  把命令写对，也不要带着\"先试试\"的心态生成没把握的命令。
  **删/写类命令（rm / del / Remove-Item / mv / cp / `>` 重定向等）执行后必须自己再
  跑一次 `ls` / `dir` / `Get-ChildItem` 验证文件确实被改动**，不能仅凭
  ``exit_code=0 + stdout 空`` 就认为成功（PowerShell 的 `Remove-Item` 删除成功时
  不输出任何内容）。
- **search_files / glob_files**：找文件用 glob，找内容用 search_files（支持正则）。
- **kb_search**：不确定某个命令用法、项目约定、历史决策时，先检索本地知识库。
- **kb_add**：把有价值的资料（文档、笔记、代码）写入知识库，之后就能被检索到。
- **沉淀本机事实（重要）**：当你弄清楚了一个**可复用的本机信息**——比如某个程序的
  启动命令与位置（「v3 在 D:\\PYTHON\\AutoEvo_agent，用 v3.bat 启动」）、项目结构约定、
  工具安装位置——**主动调用 kb_add 把它记下来**（source 写成 `env-fact:<主题>`），
  后续任务会自动检索到，不用每次重新摸索或问用户。
- **修改 PATH（重要）**：把目录加进 PATH 首选 `add_to_path` 工具（自动查重、
  按作用域读改写、广播生效）。**作用域严格按用户要求执行，不要擅自降级**：
  用户说「系统变量 Path / 所有用户 / 任何终端」→ `scope="system"`（需要管理员
  权限，工具会生成自提权脚本并**非阻塞启动**，立即返回——你收到返回后**任务即
  完成**，调用 finish_task 即可，不要重复调用或等待 UAC 结果）；
  没提作用域默认 `scope="user"`。手写命令时禁止用 `%PATH%` 展开（会把系统级
  和用户级的合并值写进单一作用域造成污染），必须先读目标作用域当前值再追加。
- **技能（Skill）**：技能库里保存的是"怎么做某类事"的成熟流程。
  上方会给出技能目录，如果你判断某个技能和当前任务相关，用 read_skill 读取它的完整内容
  并严格按它的流程执行。任务结束后系统会让你反思是否需要新增/修改技能。
- **remember / recall_memory**：稳定的用户偏好、项目约定、环境特点可以写入长期记忆。"""


SAFETY_NOTE = """## 安全

以下操作在执行前会自动请求用户确认（你不需要自己问）：
{confirm_tools}

明显破坏性的命令（格式化磁盘、删除系统目录、关机等）会被直接拒绝。
如果被拒绝，不要试图绕过，改用一个更安全的方式，或向用户说明。"""


def build_system_prompt(
    environment: dict,
    shell_kind: str = "unknown",
    *,
    plan_text: str = "",
    task_mode: str = "new_task",
    user_intent: str = "",
    skill_catalog: str = "",
    loaded_skills: str = "",
    retrieved_context: str = "",
    memory_text: str = "",
    with_tools: bool = True,
) -> str:
    """组装 agent 节点的系统提示词。"""
    blocks: list[str] = [AGENT_ROLE, ""]

    blocks.append("## 运行环境\n")
    blocks.append(environment_block(environment, shell_kind))
    blocks.append("")
    blocks.append(_shell_syntax_hint(shell_kind))
    blocks.append("")

    if with_tools:
        blocks.append(TOOL_GUIDANCE)
        blocks.append("")
        blocks.append(
            SAFETY_NOTE.format(confirm_tools=_confirm_tools_text())
        )
        blocks.append("")
        blocks.append("## 可用工具\n")
        blocks.append(describe_tools_via_registry())
        blocks.append("")

    if memory_text.strip():
        blocks.append("## 长期记忆（来自以往会话）\n")
        blocks.append(memory_text.strip())
        blocks.append("")

    if skill_catalog.strip():
        blocks.append("## 技能库目录\n")
        blocks.append(skill_catalog.strip())
        blocks.append("")

    if loaded_skills.strip():
        blocks.append("## 本回合已载入的技能（请按其中的流程执行）\n")
        blocks.append(loaded_skills.strip())
        blocks.append("")

    if retrieved_context.strip():
        blocks.append("## 知识库检索结果\n")
        blocks.append(retrieved_context.strip())
        blocks.append("")

    if user_intent.strip():
        mode_label = {
            "new_task": "全新任务",
            "adjust": "对既有任务的追加/修改",
            "proposal_response": "对技能管理建议的回应",
            "followup": "后续对话",
        }.get(task_mode, task_mode)
        blocks.append(f"## 当前任务（{mode_label}）\n")
        blocks.append(f"用户目标：{user_intent.strip()}")
        if plan_text.strip():
            blocks.append("")
            blocks.append("当前计划：")
            blocks.append(plan_text.strip())
        blocks.append("")
        blocks.append(
            "现在开始执行。需要工具就直接调用；任务完成或确认无法继续时调用 finish_task。"
        )

    return "\n".join(blocks).strip()


# ============================================================
# 规划
# ============================================================

PLANNER_SYSTEM = """你是一个任务规划器。你的职责是把用户的请求拆解成可执行的步骤。

要求：
- 步骤要具体、可执行、可验证，每条是一个动作（用哪个工具做什么）。
- 每条步骤描述控制在 40 字以内，动词开头；涉及文件内容、命令长参数等
  大段文本只用一句话概括（如「写入 print 脚本内容」「生成约 30 行配置」），
  **不要把完整内容粘进步骤描述**，具体内容留给工具参数。
- 步骤数量控制在 1~8 条；简单任务（比如只是问一个问题、读一个文件）就 1 条。
- 不要写"分析需求""思考方案"这类没有产出的步骤。
- 如果用户是在修改已有任务，保留仍然有效的已完成步骤，只调整后面部分。

【信息不足怎么办】
- **先看「运行环境」**：上下文里给了操作系统、shell 类型、当前目录内容和关键环境变量。
  这些能查到的问题（什么系统、什么 shell、当前目录有什么、路径在哪）**绝对不要问用户**。
- 规划前先扫一眼目录概览：如果当前目录有 `.bat` / `.cmd` / `.ps1` / `.exe` 等启动器，
  说明相关程序就在本地，直接在步骤里用它，不要问「程序在哪」。
- 如果缺少只有用户知道的必要信息（精确文件名、目标路径、要保留什么、跳过什么、
  多选一的选择），不要瞎猜；按下面的 JSON 格式输出 `clarify_questions` 字段，
  每项一个问题。系统会把问题抛给用户，回答后会重新让你规划。
- 如果信息可以通过工具自洽拿到（比如"找某个 1.txt"），直接在 steps 里写"用
  glob_files 找 1.txt 的精确路径"，不要为了问问题而问。

【上次失败后重规划】
- 如果上下文里有 `last_error`，必须把它纳入规划：
    - 文件/路径类失败 → 步骤里加"先 glob_files / search_files 确认路径存在再操作"
    - 命令类失败 → 步骤里换一种写法（PowerShell vs bash），或先做 dry-run
    - 同一步骤重复失败 → 拆成更小的子步骤或换工具

【命令书写约束】
- 步骤描述里涉及命令时（Windows/PowerShell 环境），**不要写出 `&&` 连接的命令**，
  多条命令用 `;` 分隔；需要 POSIX `&&` 语义时明确写"通过 `bash -lc` 执行"。
- 涉及修改 PATH 的步骤，按用户明确要求的作用域规划（用户级 user / 系统级 system）；
  系统级需管理员权限（add_to_path 工具会自动 UAC 提权），不要擅自改成用户级。

只输出 JSON，格式如下，不要输出其他任何内容：
{"summary": "一句话概述这个计划要做什么", "risks": ["风险1", "风险2"], "steps": ["步骤1", "步骤2", ...], "clarify_questions": [{"question": "...", "options": ["选项A", "选项B"], "context": "为什么要问"}]}
- ``summary``：一句话（30字内）概括计划的整体意图，让用户快速理解要做什么。
- ``risks``：列出本计划可能的风险或副作用（如"会修改系统PATH"、"会删除文件"、"需要管理员权限"）。
  没有风险时写空数组 ``[]``。
- 没有澄清问题时 `clarify_questions` 字段写成空数组 `[]`。
- 不要把澄清问题写在 steps 里（步骤里只能是动作）。"""


def build_planner_user(
    user_intent: str,
    task_mode: str,
    existing_plan: list[dict] | None = None,
    recent_context: str = "",
    retrieved_context: str = "",
    memory_text: str = "",
    env_context: str = "",
) -> str:
    """规划节点的用户提示词。"""
    parts: list[str] = []

    if env_context.strip():
        parts.append(env_context.strip())

    if existing_plan:
        lines = []
        for i, step in enumerate(existing_plan, 1):
            status = step.get("status", "pending")
            mark = {
                "done": "[已完成]",
                "failed": "[失败]",
                "skipped": "[跳过]",
                "running": "[进行中]",
            }.get(status, "[未开始]")
            lines.append(f"{i}. {mark} {step.get('description', '')}")
        parts.append("### 现有计划\n" + "\n".join(lines))

    if recent_context.strip():
        parts.append("### 最近对话\n" + recent_context.strip())

    if retrieved_context.strip():
        parts.append("### 相关知识\n" + retrieved_context.strip())

    if memory_text.strip():
        parts.append("### 长期记忆\n" + memory_text.strip())

    parts.append(f"### 本轮用户输入\n{user_intent}")
    parts.append(f"### 本轮性质\n{task_mode}")
    parts.append("请给出完整的最新计划（JSON）。")
    return "\n\n".join(parts)


# ============================================================
# 反思 / 自进化
# ============================================================

REFLECT_SYSTEM = """你是一个技能库管理员。你的职责是：在一次任务结束后，判断技能库是否需要演进。

你会收到：
  1. 技能管理器的完整工作流程（必须遵守）
  2. 本次任务的执行摘要与结果
  3. 当前技能库目录

你的输出必须是两种之一：

**A. 有建议时**，严格按技能管理器要求的格式输出：
```
【技能管理建议】
本次任务后发现 N 条可管理的技能项：

1. 建议动作：创建 / 更新 / 合并 / 删除 / 清理
   涉及技能：<技能名>
   保留技能：<仅合并时>
   归档技能：<仅合并/清理/删除时>
   原因：<引用本次任务中的具体证据>
   具体改动：<要做什么>
...
```
每条必须引用本次任务中的具体证据，不能凭空猜测。

**B. 没有建议时**，只输出一行：
`NO_PROPOSAL`

判断标准（严格遵守）：
- 任务成功且现有技能完美覆盖 → NO_PROPOSAL
- 本次任务是一次性琐事、没有复用价值 → NO_PROPOSAL
- 任务成功但流程可复用且技能库没有对应技能 → 建议创建
- 加载的技能有缺陷（缺步骤、描述匹配不上、信息过时）导致任务受阻或返工 → 建议更新
- 任务在未完成状态下结束，且原因是技能缺陷 → 建议更新
- 用户明确表达不满，且与技能缺陷相关 → 建议更新
- 发现多个技能职责重叠 → 建议合并
- 发现技能冗余/过时/矛盾 → 建议清理

不要为了显得主动而制造无意义的提议。最多 5 条建议。
你的输出会直接展示给用户，所以不要输出多余的客套话。"""


def build_reflect_user(
    skill_manager_skill: str,
    task_summary: str,
    execution_digest: str,
    skill_catalog: str,
    declined: Optional[list[str]] = None,
) -> str:
    """反思节点的用户提示词。"""
    parts = [
        "## 技能管理器工作流程\n" + skill_manager_skill.strip(),
        "",
        "## 本次任务执行摘要\n" + task_summary.strip(),
        "",
        "## 工具调用记录\n" + (execution_digest.strip() or "(无工具调用)"),
        "",
        "## 当前技能库\n" + (skill_catalog.strip() or "(技能库为空)"),
    ]
    if declined:
        parts += [
            "",
            "## 用户此前已明确拒绝的建议（不要重复提议）\n"
            + "\n".join(f"- {d}" for d in declined),
        ]
    parts += ["", "请判断是否需要管理技能，并按要求的格式输出。"]
    return "\n\n".join(parts)


# ============================================================
# 计划渲染
# ============================================================

def render_plan(plan: list[dict] | None, current_index: int = 0) -> str:
    """把计划渲染成文本。"""
    if not plan:
        return ""
    lines: list[str] = []
    for i, step in enumerate(plan):
        status = step.get("status", "pending")
        mark = {
            "done": "✅",
            "failed": "❌",
            "skipped": "⏭",
            "running": "▶",
        }.get(status, "○")
        arrow = " ← 当前" if i == current_index and status in ("pending", "running") else ""
        lines.append(f"{i + 1}. {mark} {step.get('description', '')}{arrow}")
        if step.get("error"):
            lines.append(f"      错误: {step['error']}")
    return "\n".join(lines)


def render_execution_digest(history: list[dict] | None, limit: int = 40) -> str:
    """把执行历史压缩成反思用的摘要。"""
    if not history:
        return ""
    lines: list[str] = []
    for rec in history[-limit:]:
        name = rec.get("tool_name", "?")
        args = rec.get("tool_input", {})
        arg_brief = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
        status = "被拒绝" if rec.get("blocked") else (
            "失败" if (rec.get("exit_code") is not None and rec.get("exit_code") != 0) else "成功"
        )
        lines.append(f"- {name}({arg_brief}) -> {status}")
        out = (rec.get("stdout") or "").strip().splitlines()
        if out:
            lines.append(f"    输出: {out[0][:160]}")
        err = (rec.get("stderr") or "").strip().splitlines()
        if err:
            lines.append(f"    错误: {err[0][:160]}")
    return "\n".join(lines)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


__all__ = [
    "build_system_prompt",
    "environment_block",
    "PLANNER_SYSTEM",
    "build_planner_user",
    "REFLECT_SYSTEM",
    "build_reflect_user",
    "render_plan",
    "render_execution_digest",
    "utc_now_text",
]
