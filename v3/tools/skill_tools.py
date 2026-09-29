"""技能管理工具。

供 agent 在用户主动要求、或反思节点提议通过后，对技能库执行增删改查 / 合并 / 精简 / 审计。
所有写操作都默认走 ``requires_confirm=True``，因为技能是 agent 自我迭代的核心。
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ..graph.llm import get_llm
from ..skillkit import (
    PROTECTED_SKILLS,
    get_skill_library,
)
from ._base import register

log = logging.getLogger(__name__)

# 写操作默认要求确认，避免误删或误改用户沉淀的 skill
_SKILL_WRITE_CONFIRM = True


# ============================================================
# 读
# ============================================================

@register(category="skills")
def list_skills() -> str:
    """列出技能库中的全部技能（名称 + 描述 + 行数）。

    在创建新技能前用它查重；在反思阶段用它对照技能库生成建议。
    """
    lib = get_skill_library()
    skills = lib.list()
    if not skills:
        return "(技能库为空)"
    lines = [f"共 {len(skills)} 个技能："]
    for s in skills:
        desc = " ".join(s.description.split()) or "(无描述)"
        lines.append(f"- {s.name} ({s.line_count} 行): {desc}")
    archived = lib.archived()
    if archived:
        lines.append(f"\n已归档 {len(archived)} 个：{', '.join(archived[:10])}")
    return "\n".join(lines)


@register(category="skills")
def read_skill(name: str) -> str:
    """读取某个技能的完整内容（含 frontmatter 与正文）。

    Args:
        name: 技能名。
    """
    lib = get_skill_library()
    s = lib.load(name)
    if not s:
        return f"错误：技能不存在 -> {name}。可用 list_skills 查看全部技能。"
    return (
        f"name: {s.name}\n"
        f"description: {s.description}\n"
        f"行数: {s.line_count}\n"
        f"路径: {s.path}\n"
        "---\n"
        f"{s.body}"
    )


@register(category="skills")
def search_skills(query: str, limit: int = 5) -> str:
    """按关键词检索技能（匹配名称、描述、正文）。

    Args:
        query: 检索关键词。
        limit: 最多返回几条。
    """
    lib = get_skill_library()
    hits = lib.search(query, limit=int(limit or 5))
    if not hits:
        return f"未找到与 {query!r} 相关的技能"
    return "\n".join(
        f"- {s.name} ({s.line_count} 行): {' '.join(s.description.split())[:160]}" for s in hits
    )


# ============================================================
# 写
# ============================================================

@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="medium")
def create_skill(name: str, description: str, body: str) -> str:
    """创建一个新技能。

    Args:
        name: 技能名，snake_case，如 "deploy_docker_container"。
        description: 技能描述。**必须包含具体触发短语**（用户会怎么说），
            否则以后匹配不到。例如"部署 Docker 容器。当用户要求部署/运行 docker 容器时使用"。
        body: Markdown 正文。建议包含：何时使用、工作流步骤、示例、边界情况、工具依赖。
    """
    lib = get_skill_library()
    key = lib.normalize_name(name)
    if not key:
        return "错误：技能名非法，请使用 snake_case（字母/数字/下划线）"
    if key in PROTECTED_SKILLS:
        return f"错误：{key} 是受保护技能，不能通过工具创建或覆盖"
    if lib.exists(key):
        return (
            f"错误：技能 {key} 已存在。如需修改请用 update_skill；"
            "如果想换个名字，请先与用户确认。"
        )
    if not (description or "").strip():
        return "错误：description 不能为空，且必须包含触发短语"
    from ..skillkit import MAX_SKILL_BODY_LINES
    if len((body or "").splitlines()) > MAX_SKILL_BODY_LINES:
        return (
            f"错误：正文超过 {MAX_SKILL_BODY_LINES} 行。"
            "请拆分到 references/ 子目录并在正文中引用。"
        )

    lib.save(key, description, body)
    return f"[SKILL] created: {key}"


@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="medium")
def update_skill(name: str, description: str = "", body: str = "") -> str:
    """更新已有技能。只传入需要修改的部分；未传入的字段保持原值。

    Args:
        name: 技能名。
        description: 新的描述（留空则不变）。
        body: 新的正文（留空则不变）。
    """
    lib = get_skill_library()
    key = lib.normalize_name(name)
    if key in PROTECTED_SKILLS:
        return f"错误：{key} 是受保护技能，未经用户明确指示不得修改"

    existing = lib.load(key)
    if not existing:
        return f"错误：技能不存在 -> {key}。创建请用 create_skill。"

    new_desc = (description or "").strip() or existing.description
    new_body = (body or "").strip() or existing.body

    from ..skillkit import MAX_SKILL_BODY_LINES
    if len(new_body.splitlines()) > MAX_SKILL_BODY_LINES:
        return f"错误：正文超过 {MAX_SKILL_BODY_LINES} 行，请拆分到 references/"

    lib.save(key, new_desc, new_body, existing.meta)
    return f"[SKILL] updated: {key}"


@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="high")
def delete_skill(name: str, archive: bool = True) -> str:
    """删除技能。默认归档到 skills/.archive/（可恢复）而不是物理删除。

    Args:
        name: 技能名。
        archive: True 归档（推荐），False 物理删除。
    """
    lib = get_skill_library()
    key = lib.normalize_name(name)
    if key in PROTECTED_SKILLS:
        return f"错误：{key} 是受保护技能，不能删除"
    if not lib.exists(key):
        return f"错误：技能不存在 -> {key}"

    ok = lib.delete(key, archive=bool(archive))
    if not ok:
        return f"错误：删除失败 -> {key}"
    return f"[SKILL] {'archived' if archive else 'deleted'}: {key}"


@register(category="skills")
def list_archived_skills() -> str:
    """列出已归档的技能，便于恢复或彻底清理。"""
    lib = get_skill_library()
    names = lib.archived()
    if not names:
        return "(没有已归档的技能)"
    return "已归档技能：\n" + "\n".join(f"- {n}" for n in names)


@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="medium")
def restore_skill(archived_name: str) -> str:
    """把已归档的技能恢复回技能库。

    Args:
        archived_name: list_archived_skills 返回的归档目录名。
    """
    lib = get_skill_library()
    restored = lib.restore(archived_name)
    if not restored:
        return f"错误：无法恢复 -> {archived_name}（不存在或同名技能已存在）"
    return f"[SKILL] restored: {restored}"


# ============================================================
# 自进化增强：合并 / 精简 / 审计 / 从历史创建
# ============================================================

@register(category="skills")
def audit_skills() -> str:
    """审计技能库：每个技能的体量、最近修改、最近被载入的频率等指标。

    用于在反思 / 用户主动要求时判断哪些技能可以精简 / 合并 / 归档。
    """
    lib = get_skill_library()
    metrics = lib.audit()
    if not metrics:
        return "(技能库为空)"
    lines = ["技能审计报告：", ""]
    for m in metrics:
        flag = []
        if m["size_lines"] > 200:
            flag.append("⚠内容偏长")
        if m["active_recent"] == 0:
            flag.append("⚠近期未使用")
        if m.get("fuzzy_match_count", 0) >= 2:
            flag.append("⚠与其他技能相似")
        flag_text = " ".join(flag) or "正常"
        lines.append(f"- {m['name']} ({m['size_lines']} 行) [{flag_text}]")
        lines.append(
            f"    最近载入: {m['active_recent']} 次, "
            f"描述长度: {len(m['description'])} 字符, "
            f"修改于: {m['mtime']}"
        )
    return "\n".join(lines)


_MERGE_SYSTEM = """你是技能合并专家。

把给定的多个技能合并成一个新技能。要求：
  - 名称（snake_case）放在 frontmatter 的 name 字段
  - description 必须保留原有技能的**触发短语**（用户会怎么说），并列出新名字
  - body 用统一的结构：概述、何时使用、工作流、示例、注意事项、依赖
  - 如果多个技能功能差异很大，给出"分段说明"而不是硬塞在一起
  - 合并后总行数不得超过 500
输出格式：
  ---
  name: <新技能名>
  description: <触发短语 + 简介>
  ---
  <body>
只输出这一个合并后的技能内容，不要其他解释。
"""


@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="medium")
def consolidate_skills(
    source_names: list[str],
    new_name: str,
    new_description: str = "",
    archive_old: bool = True,
) -> str:
    """把多个技能合并成一个新技能，旧的归档。

    适用于功能重叠、流程重复的小型技能。

    Args:
        source_names: 要合并的技能名列表。
        new_name: 合并后新技能的名称（snake_case）。
        new_description: 新技能的描述（必须包含触发短语）；留空则让 LLM 自动起草。
        archive_old: 是否把原技能归档（默认 True，可恢复）。
    """
    import json
    from ..skillkit import MAX_SKILL_BODY_LINES

    lib = get_skill_library()
    sources = []
    for name in source_names or []:
        key = lib.normalize_name(name)
        if key in PROTECTED_SKILLS:
            return f"错误：{key} 是受保护技能，不允许合并"
        s = lib.load(key)
        if not s:
            return f"错误：找不到技能 {key}"
        sources.append(s)
    if not sources:
        return "错误：source_names 不能为空"
    if len(sources) < 2:
        return "错误：合并至少要 2 个源技能"

    new_key = lib.normalize_name(new_name)
    if not new_key:
        return "错误：new_name 非法"
    if new_key in PROTECTED_SKILLS:
        return f"错误：{new_key} 是受保护技能"
    if lib.exists(new_key):
        return f"错误：技能 {new_key} 已存在，请先删除或换名"

    combined = "\n\n---\n\n".join(
        f"# {s.name}\n\n> {s.description}\n\n{s.body}"
        for s in sources
    )

    desc_prompt = (
        (new_description or "").strip()
        or "请根据以下技能合并出一个简洁的 description，必须保留原有触发短语。"
    )

    llm = get_llm()
    try:
        resp = llm.invoke(
            [
                SystemMessage(content=_MERGE_SYSTEM),
                HumanMessage(content=f"{desc_prompt}\n\n源技能：\n{combined}"),
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:
        return f"错误：LLM 调用失败 -> {type(e).__name__}: {e}"

    # 拆 frontmatter / body
    from ..skillkit import parse_skill_md
    meta, body = parse_skill_md(text, fallback_name=new_key)
    final_name = meta.get("name") or new_key
    final_desc = (meta.get("description") or desc_prompt).strip()
    final_body = (body or "").strip() or combined

    if len(final_body.splitlines()) > MAX_SKILL_BODY_LINES:
        return (
            f"错误：合并后正文 {len(final_body.splitlines())} 行，"
            f"超过 {MAX_SKILL_BODY_LINES} 行上限。请先精简各源技能后再来合并。"
        )

    lib.save(final_name, final_desc, final_body)

    archived = []
    if archive_old:
        for s in sources:
            lib.delete(s.name, archive=False)
            archived.append(s.name)
        # 真正归档到 .archive
        for s in sources:
            lib.archive_with_reason(s.name, reason=f"merged_into:{final_name}")

    return json.dumps(
        {
            "created": final_name,
            "description": final_desc,
            "lines": len(final_body.splitlines()),
            "archived": archived,
        },
        ensure_ascii=False,
    )


_REFINE_SYSTEM = """你是技能精简/重写专家。

按用户的具体要求改写给定的技能正文。要求：
  - 输出格式保持与源一致（YAML frontmatter + Markdown）
  - 保留所有触发短语与核心工作流
  - 用户要求"精简"时主动删除冗余/重复/与已有 skill_manager 内容重叠的部分
  - 用户要求"修改"时只动用户点名的章节
  - 改写后正文不超过 500 行
  - 只输出重写后的整篇技能，不要其他解释。
"""


@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="medium")
def refine_skill(name: str, instructions: str) -> str:
    """按用户指令改写某个技能（精简、合并段落、补充示例等）。

    Args:
        name: 要修改的技能名。
        instructions: 用户的改写指令，例如"精简掉与 skill_manager 重叠的部分"、
            "把工作流拆成两步"、"增加一份最小可执行示例"。
    """
    from ..skillkit import MAX_SKILL_BODY_LINES

    lib = get_skill_library()
    key = lib.normalize_name(name)
    if key in PROTECTED_SKILLS:
        return f"错误：{key} 是受保护技能，未经明确授权不得修改"
    src = lib.load(key)
    if not src:
        return f"错误：技能不存在 -> {key}"

    src_text = src.to_markdown()
    llm = get_llm()
    try:
        resp = llm.invoke(
            [
                SystemMessage(content=_REFINE_SYSTEM),
                HumanMessage(
                    content=(
                        f"## 改写要求\n{instructions.strip()}\n\n"
                        f"## 当前技能\n{src_text}"
                    )
                ),
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:
        return f"错误：LLM 调用失败 -> {type(e).__name__}: {e}"

    from ..skillkit import parse_skill_md
    meta, body = parse_skill_md(text, fallback_name=key)
    final_name = meta.get("name") or key
    final_desc = (meta.get("description") or src.description).strip()
    final_body = (body or "").strip() or src.body

    if len(final_body.splitlines()) > MAX_SKILL_BODY_LINES:
        return f"错误：改写后超过 {MAX_SKILL_BODY_LINES} 行，请拆 references/"

    lib.save(final_name, final_desc, final_body)
    return (
        f"[SKILL] refined: {final_name} ({len(src.body.splitlines())} → "
        f"{len(final_body.splitlines())} 行)"
    )


_CREATE_FROM_HISTORY_SYSTEM = """你是技能提炼专家。

从一段历史任务对话里提炼出可复用的技能。要求：
  - 触发短语（用户会怎么说）写进 description
  - body 用结构化 markdown：概述、何时使用、工作流步骤、注意事项、边界情况
  - 提炼成"以后遇到类似任务该怎么走"的流程，而不是描述这一次发生了什么
  - 正文不超过 500 行
只输出一个完整技能（YAML frontmatter + Markdown）。
"""


@register(category="skills", requires_confirm=_SKILL_WRITE_CONFIRM, risk="medium")
def create_skill_from_history(
    conversation_id: str,
    skill_name: str,
    hint: str = "",
) -> str:
    """从一段历史会话里提炼出一个新技能。

    适用于：「这个任务我以后还要做，能不能把它变成一个 skill？」

    Args:
        conversation_id: 会话 id（用 list_conversations 查看）。
        skill_name: 新技能名（snake_case）。
        hint: 用户的额外要求，例如"重点提炼调试步骤"、"忽略中途的小修小补"。
    """
    from ..skillkit import MAX_SKILL_BODY_LINES

    from ..memory import get_conversation_store

    store = get_conversation_store()
    msgs = store.get_messages(conversation_id, limit=200)
    if not msgs:
        return f"错误：会话 {conversation_id} 没有可提炼的消息"

    lib = get_skill_library()
    new_key = lib.normalize_name(skill_name)
    if not new_key:
        return "错误：skill_name 非法"
    if new_key in PROTECTED_SKILLS:
        return f"错误：{new_key} 是受保护技能"
    if lib.exists(new_key):
        return f"错误：技能 {new_key} 已存在"

    # 把对话拼成可读文本
    transcript_lines = []
    for m in msgs:
        role = m.role or "?"
        content = (m.content or "").strip()
        if not content:
            continue
        transcript_lines.append(f"[{role}] {content[:400]}")
    transcript = "\n".join(transcript_lines[-200:])

    user_prompt = (
        f"## 提炼要求\n"
        f"新技能名: {new_key}\n"
        f"{('补充要求: ' + hint) if hint else ''}\n\n"
        f"## 历史对话\n{transcript}"
    )

    llm = get_llm()
    try:
        resp = llm.invoke(
            [
                SystemMessage(content=_CREATE_FROM_HISTORY_SYSTEM),
                HumanMessage(content=user_prompt),
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:
        return f"错误：LLM 调用失败 -> {type(e).__name__}: {e}"

    from ..skillkit import parse_skill_md
    meta, body = parse_skill_md(text, fallback_name=new_key)
    final_name = meta.get("name") or new_key
    final_desc = (meta.get("description") or "").strip()
    final_body = (body or "").strip()

    if not final_desc:
        return "错误：提炼失败——LLM 没给出有效 description"
    if not final_body:
        return "错误：提炼失败——LLM 没给出有效 body"
    if len(final_body.splitlines()) > MAX_SKILL_BODY_LINES:
        return f"错误：提炼出的正文超过 {MAX_SKILL_BODY_LINES} 行"

    lib.save(final_name, final_desc, final_body)
    return f"[SKILL] created-from-history: {final_name} ({len(final_body.splitlines())} 行)"


# 兼容旧导入
def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


SKILL_TOOLS = [
    "list_skills",
    "read_skill",
    "search_skills",
    "create_skill",
    "update_skill",
    "delete_skill",
    "list_archived_skills",
    "restore_skill",
    "audit_skills",
    "consolidate_skills",
    "refine_skill",
    "create_skill_from_history",
]

__all__ = [
    "list_skills",
    "read_skill",
    "search_skills",
    "create_skill",
    "update_skill",
    "delete_skill",
    "list_archived_skills",
    "restore_skill",
    "audit_skills",
    "consolidate_skills",
    "refine_skill",
    "create_skill_from_history",
    "SKILL_TOOLS",
]