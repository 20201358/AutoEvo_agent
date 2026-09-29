"""技能库（Skill Library）。

技能 = 一个目录 ``skills/<name>/SKILL.md``，YAML frontmatter + Markdown 正文。

    ---
    name: deploy_docker
    description: 部署 Docker 容器。当用户要求"部署/运行 docker 容器"时使用。
    ---
    # 正文：何时使用、工作流、示例、边界情况

本模块提供：
    - 解析 / 序列化（frontmatter + 正文）
    - 增删改查、归档
    - 关键词检索（含中文分词兜底）与向量检索（可选）
    - 技能列表快照（注入 prompt 用）
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .config import settings

log = logging.getLogger(__name__)

PROTECTED_SKILLS = {"skill_manager"}
"""不允许删除 / 重命名的技能。"""

MAX_SKILL_BODY_LINES = 500
"""超过该行数建议拆分到 references/。"""


# ============================================================
# 数据结构
# ============================================================

@dataclass
class Skill:
    """一个技能。"""

    name: str
    description: str = ""
    body: str = ""
    meta: dict = field(default_factory=dict)
    path: Optional[Path] = None
    mtime: float = 0.0
    size: int = 0

    @property
    def line_count(self) -> int:
        return len(self.body.splitlines())

    def frontmatter(self) -> str:
        """序列化 frontmatter。"""
        data = {"name": self.name, "description": self.description}
        for k, v in self.meta.items():
            if k not in data:
                data[k] = v
        try:
            import yaml

            dumped = yaml.safe_dump(
                data, allow_unicode=True, sort_keys=False, default_flow_style=False
            ).strip()
        except ImportError:  # pragma: no cover
            lines = [f"name: {self.name}", f"description: {self.description}"]
            dumped = "\n".join(lines)
        return f"---\n{dumped}\n---\n"

    def to_markdown(self) -> str:
        return f"{self.frontmatter()}\n{self.body.strip()}\n"

    def summary(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "lines": self.line_count,
            "path": str(self.path) if self.path else None,
        }

    def __repr__(self) -> str:
        return f"<Skill {self.name} lines={self.line_count}>"


# ============================================================
# 解析 / 序列化
# ============================================================

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_skill_md(text: str, fallback_name: str = "") -> tuple[dict, str]:
    """拆出 (frontmatter_dict, body)。

    frontmatter 缺失或非法时不抛异常，返回空 dict + 全文。
    """
    text = (text or "").lstrip("\ufeff")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {"name": fallback_name}, text.strip()

    raw_meta = m.group(1)
    body = text[m.end() :].strip()

    meta: dict = {}
    try:
        import yaml

        loaded = yaml.safe_load(raw_meta)
        if isinstance(loaded, dict):
            meta = {str(k): v for k, v in loaded.items()}
    except Exception as e:
        log.warning("frontmatter 解析失败，按纯文本处理: %s", e)
        meta = {"name": fallback_name, "_parse_error": str(e)}

    if not meta.get("name"):
        meta["name"] = fallback_name
    return meta, body


# ============================================================
# 技能库
# ============================================================

class SkillLibrary:
    """磁盘上的技能库。"""

    def __init__(self, root: Optional[str | Path] = None):
        self.root = Path(root or settings.SKILLS_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 用量统计：name -> {load_count, last_loaded_at, last_referenced_at}
        # 进程内维护，用于 audit / 自进化建议
        self._usage: dict[str, dict] = {}

    # ---------------- 路径 ----------------

    def _skill_dir(self, name: str) -> Path:
        return self.root / name

    def _skill_file(self, name: str) -> Path:
        return self._skill_dir(name) / "SKILL.md"

    @staticmethod
    def normalize_name(name: str) -> str:
        """把用户输入的名字规范化为 snake_case 目录名。"""
        name = (name or "").strip().lower()
        name = re.sub(r"[\s\-]+", "_", name)
        name = re.sub(r"[^0-9a-z_\u4e00-\u9fff]", "", name)
        name = re.sub(r"_+", "_", name).strip("_")
        return name

    # ---------------- 读取 ----------------

    def exists(self, name: str) -> bool:
        return self._skill_file(self.normalize_name(name)).exists()

    def names(self) -> list[str]:
        """列出全部技能名（含已归档的排除在外）。"""
        out: list[str] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / "SKILL.md").exists() or (child / "skill.md").exists():
                out.append(child.name)
        return out

    def load(self, name: str) -> Optional[Skill]:
        """读取单个技能。"""
        key = self.normalize_name(name)
        # 用量计数
        with self._lock:
            u = self._usage.setdefault(
                key, {"load_count": 0, "last_loaded_at": 0.0}
            )
            u["load_count"] += 1
            u["last_loaded_at"] = datetime.now(timezone.utc).timestamp()
        for fname in ("SKILL.md", "skill.md", "SKILL.markdown"):
            path = self._skill_dir(key) / fname
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    log.warning("读取技能失败 %s: %s", path, e)
                    return None
                meta, body = parse_skill_md(text, fallback_name=key)
                stat = path.stat()
                return Skill(
                    name=str(meta.get("name") or key),
                    description=str(meta.get("description") or ""),
                    body=body,
                    meta=meta,
                    path=path,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                )

        # 目录名不匹配但存在任意 md：兜底
        d = self._skill_dir(key)
        if d.is_dir():
            candidates = sorted(d.glob("*.md"))
            if candidates:
                path = candidates[0]
                text = path.read_text(encoding="utf-8", errors="replace")
                meta, body = parse_skill_md(text, fallback_name=key)
                return Skill(
                    name=str(meta.get("name") or key),
                    description=str(meta.get("description") or ""),
                    body=body,
                    meta=meta,
                    path=path,
                )
        return None

    def list(self) -> list[Skill]:
        """列出全部技能（按名称排序）。"""
        skills: list[Skill] = []
        for name in self.names():
            s = self.load(name)
            if s:
                skills.append(s)
        return skills

    def snapshot(self) -> list[dict]:
        """返回技能摘要列表（注入 prompt 用）。"""
        return [s.summary() for s in self.list()]

    # ---------------- 写入 ----------------

    def save(
        self,
        name: str,
        description: str,
        body: str,
        extra_meta: Optional[dict] = None,
    ) -> Skill:
        """创建或覆盖技能。"""
        key = self.normalize_name(name)
        if not key:
            raise ValueError("技能名不能为空")

        skill = Skill(
            name=key,
            description=(description or "").strip(),
            body=(body or "").strip(),
            meta=dict(extra_meta or {}),
        )

        with self._lock:
            d = self._skill_dir(key)
            d.mkdir(parents=True, exist_ok=True)
            path = d / "SKILL.md"
            path.write_text(skill.to_markdown(), encoding="utf-8")
            skill.path = path
            skill.size = path.stat().st_size
        log.info("[SKILL] saved: %s", key)
        return skill

    def rename(self, old: str, new: str) -> bool:
        """重命名技能目录并同步 frontmatter 的 name。"""
        old_key, new_key = self.normalize_name(old), self.normalize_name(new)
        if old_key in PROTECTED_SKILLS or new_key in PROTECTED_SKILLS:
            return False
        src, dst = self._skill_dir(old_key), self._skill_dir(new_key)
        if not src.exists() or dst.exists():
            return False

        with self._lock:
            src.rename(dst)
            skill = self.load(new_key)
            if skill:
                skill.name = new_key
                self.save(new_key, skill.description, skill.body, skill.meta)
        log.info("[SKILL] renamed: %s -> %s", old_key, new_key)
        return True

    def delete(self, name: str, archive: bool = True) -> bool:
        """删除技能。默认先归档到 ``skills/.archive/``。"""
        key = self.normalize_name(name)
        if key in PROTECTED_SKILLS:
            log.warning("拒绝删除受保护技能: %s", key)
            return False

        src = self._skill_dir(key)
        if not src.exists():
            return False

        with self._lock:
            if archive:
                archive_root = self.root / ".archive"
                archive_root.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                dst = archive_root / f"{key}-{stamp}"
                shutil.move(str(src), str(dst))
                log.info("[SKILL] archived: %s -> %s", key, dst.name)
            else:
                shutil.rmtree(src, ignore_errors=True)
                log.info("[SKILL] deleted: %s", key)
        return True

    def archived(self) -> list[str]:
        """列出已归档的技能目录名。"""
        archive_root = self.root / ".archive"
        if not archive_root.exists():
            return []
        return sorted(p.name for p in archive_root.iterdir() if p.is_dir())

    def restore(self, archived_name: str) -> Optional[str]:
        """把归档的技能恢复回来。"""
        src = self.root / ".archive" / archived_name
        if not src.is_dir():
            return None
        target_name = re.sub(r"-\d{8}-\d{6}$", "", archived_name)
        dst = self._skill_dir(target_name)
        if dst.exists():
            return None
        shutil.move(str(src), str(dst))
        return target_name

    def archive_with_reason(self, name: str, reason: str = "") -> bool:
        """归档一个技能并在归档目录里留一份 manifest（用于追溯）。"""
        key = self.normalize_name(name)
        if key in PROTECTED_SKILLS:
            return False
        src = self._skill_dir(key)
        if not src.exists():
            return False
        with self._lock:
            archive_root = self.root / ".archive"
            archive_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            dst = archive_root / f"{key}-{stamp}"
            shutil.move(str(src), str(dst))
            try:
                manifest = {
                    "name": key,
                    "archived_at": stamp,
                    "reason": reason,
                }
                (dst / "ARCHIVE.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                log.debug("写入归档 manifest 失败: %s", e)
            log.info("[SKILL] archived(reason=%s): %s -> %s", reason, key, dst.name)
        return True

    # ---------------- 检索 ----------------

    def search(self, query: str, limit: int = 5) -> list[Skill]:
        """关键词检索：对 name + description + body 做加权词面匹配。"""
        q = (query or "").strip().lower()
        if not q:
            return []
        tokens = _tokenize(q)
        if not tokens:
            return []

        scored: list[tuple[float, Skill]] = []
        for skill in self.list():
            score = 0.0
            name_l = skill.name.lower().replace("_", " ")
            desc_l = skill.description.lower()
            body_l = skill.body.lower()

            for tok in tokens:
                if tok in name_l:
                    score += 3.0
                if tok in desc_l:
                    score += 2.0
                score += 0.5 * body_l.count(tok)

            if q in name_l or q in desc_l:
                score += 5.0

            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [s for _, s in scored[:limit]]

    # ---------------- 提示词注入 ----------------

    def render_catalog(self, max_skills: int = 60) -> str:
        """渲染技能目录（名称 + 描述），供 agent 决定是否加载。"""
        skills = self.list()
        if not skills:
            return "(技能库为空)"
        lines = []
        for s in skills[:max_skills]:
            desc = " ".join(s.description.split())
            lines.append(f"- {s.name}: {desc or '(无描述)'}")
        if len(skills) > max_skills:
            lines.append(f"... 另有 {len(skills) - max_skills} 个技能")
        return "\n".join(lines)

    def render_full(self, names: Sequence[str], max_chars_per_skill: int = 4000) -> str:
        """渲染指定技能的完整内容（渐进披露：按需加载）。"""
        blocks: list[str] = []
        for name in names:
            s = self.load(name)
            if not s:
                continue
            body = s.body
            if len(body) > max_chars_per_skill:
                body = body[:max_chars_per_skill] + "\n…(已截断)"
            blocks.append(
                f"### 技能: {s.name}\n{s.description}\n\n{body}"
            )
        return "\n\n---\n\n".join(blocks) if blocks else ""

    def stats(self) -> dict:
        skills = self.list()
        return {
            "skills": len(skills),
            "archived": len(self.archived()),
            "root": str(self.root),
            "total_lines": sum(s.line_count for s in skills),
        }

    # ---------------- 自进化 ----------------

    def audit(self) -> list[dict]:
        """返回每个技能的硬指标，供 agent 决定精简 / 合并 / 归档。

        每项：
            name, size_lines (size), chars, description, mtime, mtime_text,
            load_count, last_loaded_at, fuzzy_match_count（描述与多少个其他技能
            共享词条越多越可能冗余）
        """
        skills = self.list()
        # 准备描述级 token 用于查重
        descriptions = [(s.name, _tokenize((s.description or "").lower())) for s in skills]

        out: list[dict] = []
        for s in skills:
            u = self._usage.get(s.name, {})
            other_count = 0
            mine = _tokenize((s.description or "").lower())
            if mine:
                for other_name, other_tokens in descriptions:
                    if other_name == s.name or not other_tokens:
                        continue
                    if mine & other_tokens:
                        other_count += 1
            mtime_text = (
                datetime.fromtimestamp(s.mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if s.mtime
                else "(未知)"
            )
            out.append(
                {
                    "name": s.name,
                    "size_lines": s.line_count,
                    "chars": s.size,
                    "description": s.description,
                    "mtime": mtime_text,
                    "load_count": int(u.get("load_count", 0)),
                    "last_loaded_at": (
                        datetime.fromtimestamp(u["last_loaded_at"], timezone.utc).strftime(
                            "%Y-%m-%d %H:%M UTC"
                        )
                        if u.get("last_loaded_at")
                        else "(从未载入)"
                    ),
                    "fuzzy_match_count": other_count,
                }
            )
        # 优先按 size_lines desc
        out.sort(key=lambda r: (-r["size_lines"], r["name"]))
        return out

    def consolidate(
        self,
        sources: Sequence[str],
        target: str,
        body: str,
        description: str = "",
        archive_old: bool = True,
        archive_reason: str = "consolidated",
    ) -> Skill:
        """把多个技能合并为一个新技能。

        与 ``tools.consolidate_skills`` 的区别：本方法不做 LLM 调用，body /
        description 由调用方准备好。LLM 版走 ``tools.consolidate_skills``。
        """
        target_key = self.normalize_name(target)
        if target_key in PROTECTED_SKILLS:
            raise ValueError(f"{target_key} 是受保护技能")
        if self.exists(target_key):
            raise ValueError(f"目标技能 {target_key} 已存在")

        skill = self.save(target_key, description, body)

        if archive_old:
            for name in sources or []:
                key = self.normalize_name(name)
                if key == target_key:
                    continue
                if key in PROTECTED_SKILLS:
                    log.warning("合并时跳过受保护技能: %s", key)
                    continue
                self.archive_with_reason(key, reason=f"{archive_reason}->{target_key}")
        return skill

    def refine(self, name: str, body: str, description: str = "") -> Skill:
        """直接以新内容覆盖一个技能。LLM 版走 ``tools.refine_skill``。"""
        key = self.normalize_name(name)
        if key in PROTECTED_SKILLS:
            raise ValueError(f"{key} 是受保护技能")
        existing = self.load(key)
        if not existing:
            raise ValueError(f"技能不存在: {key}")
        new_desc = (description or "").strip() or existing.description
        return self.save(key, new_desc, body, existing.meta)

    def __repr__(self) -> str:
        return f"<SkillLibrary root={self.root} skills={len(self.names())}>"


# ============================================================
# 分词（中英混排兜底）
# ============================================================

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """英文按词、中文按字切分，并补充二元组。"""
    raw = _TOKEN_RE.findall(text.lower())
    out = list(raw)
    for a, b in zip(raw, raw[1:]):
        out.append(a + b)
    return out


# ============================================================
# 单例
# ============================================================

_lib_lock = threading.Lock()
_library: Optional[SkillLibrary] = None


def get_skill_library(force_new: bool = False) -> SkillLibrary:
    """获取全局技能库。"""
    global _library
    with _lib_lock:
        if _library is None or force_new:
            _library = SkillLibrary()
        return _library


def reset_skill_library() -> None:
    """丢弃全局技能库（配置变更后调用）。"""
    global _library
    with _lib_lock:
        _library = None


__all__ = [
    "Skill",
    "SkillLibrary",
    "parse_skill_md",
    "get_skill_library",
    "reset_skill_library",
    "PROTECTED_SKILLS",
    "MAX_SKILL_BODY_LINES",
]


# 兼容老 API（反射时偶尔会用）
def audit_skills_via_llm():
    """保留占位；真正 LLM 审计在 tools.refine_skill / consolidate_skills 里做。"""
    raise NotImplementedError("改用 v3.tools.skill_tools.consolidate_skills / refine_skill")
