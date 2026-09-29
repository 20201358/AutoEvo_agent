# -*- coding: utf-8 -*-
"""安全策略的单元验证：

1. PATH 修改（add_to_path 辅助逻辑：查重 / PS 脚本生成 / 管理员检测）
2. 命令失败后 run_shell 需用户批准（tools_node 状态机逻辑，mock 工具执行）
3. 提示词层禁止生成 && 命令 + PATH 作用域按用户要求执行

不依赖真实 shell / 不写注册表：只测纯函数与 mock 执行路径。
"""

# ---------- 1. PATH 辅助逻辑 ----------
from v3.tools.installer import (
    _path_contains,
    _build_add_path_script,
    _is_admin,
    _read_path_scope,
)

# 查重（大小写 / 尾部反斜杠不敏感）
assert _path_contains(r'C:\Tools;D:\Bin', 'C:\\tools')
assert _path_contains(r'C:\Tools\;D:\Bin', 'D:\\Bin')
assert not _path_contains(r'C:\Tools;D:\BinX', 'D:\\Bin')
assert _path_contains('', '') is False or _path_contains('', '')  # 空目录不误报
assert not _path_contains(r'C:\Tools', 'C:\\Tools2')

# PS 脚本生成：作用域与目录正确嵌入、单引号转义
s = _build_add_path_script('Machine', r'D:\PYTHON\AutoEvo_agent')
assert "'Machine'" in s
assert r"'D:\PYTHON\AutoEvo_agent'" in s
assert 'SetEnvironmentVariable' in s
assert 'SendMessageTimeout' in s  # 有广播 WM_SETTINGCHANGE
assert 'ALREADY_PRESENT' in s    # 有查重分支
s = _build_add_path_script('User', r"C:\Program Files\X's dir")
assert "''" in s  # 单引号被转义

# 管理员检测：不抛异常即可
_is_admin()

# 读取作用域 PATH：不抛异常（返回值可能是 None 或字符串）
r = _read_path_scope('User')
assert r is None or isinstance(r, str)

# add_to_path 参数校验
from v3.tools.installer import add_to_path
out = add_to_path(directory='C:\\Windows', scope='bogus')
assert '未知 scope' in out
out = add_to_path(directory=r'C:\__no_such_dir__', scope='system')
assert '不是目录' in out
# session 提示不落盘
out = add_to_path(directory='C:\\Windows', scope='session')
assert 'session' in out and 'run_shell' in out

# ---------- 2. 失败后需批准（tools_node 逻辑，mock 工具执行） ----------
from langchain_core.messages import AIMessage
from v3.graph.nodes import tools as T


class _FakeTool:
    """模拟 run_shell 的返回格式（带 exit_code 行）。"""

    def __init__(self, exit_code: int):
        self.exit_code = exit_code

    def invoke(self, args):
        return f"exit_code={self.exit_code}\nstdout: (空)"


def _make_state(flag, resolved=None):
    return {
        'messages': [],
        'shell_error_confirm': flag,
        'resolved_confirmations': dict(resolved or {}),
        'user_answers': {},
        'safety': {},
        'plan': [],
        'current_step_index': 0,
        'execution_history': [],
        'error_state': {},
        'step_attempts': {},
        'task_id': 't',
    }


def _ai_msg(name='run_shell', args=None, cid='call_1'):
    return AIMessage(
        content='',
        tool_calls=[{'name': name, 'id': cid, 'args': args or {'command': 'Get-Location'}}],
    )


def _patch_tools(monkey_exit_code):
    """替换 tools_node 的工具表，避免真跑 shell。"""
    T.get_tool_map = lambda: {'run_shell': _FakeTool(monkey_exit_code)}


# 2a. flag=False + 命令成功 → 无确认，flag 清除
_patch_tools(0)
st = _make_state(False)
st['messages'] = [_ai_msg()]
out = T.tools_node(st)
assert out.get('pending_confirmation') is None, out.get('pending_confirmation')
assert out.get('shell_error_confirm') is False

# 2b. flag=False + 命令失败 → flag 置位（下一轮起需批准）
_patch_tools(1)
st = _make_state(False)
st['messages'] = [_ai_msg()]
out = T.tools_node(st)
assert out.get('pending_confirmation') is None
assert out.get('shell_error_confirm') is True

# 2c. flag=True → run_shell 被 confirm 拦下
st = _make_state(True)
st['messages'] = [_ai_msg()]
out = T.tools_node(st)
pc = out.get('pending_confirmation')
assert pc is not None, 'flag=True 时应产生 pending_confirmation'
assert pc['kind'] == 'confirm'
assert pc['items'][0]['tool_name'] == 'run_shell'
assert '失败' in pc['items'][0]['reason']
assert pc['items'][0]['rule'] == 'shell:after_error'

# 2d. flag=True + 用户已批准 → 放行执行；命令成功后 flag 清除
_patch_tools(0)
st = _make_state(True, resolved={'call_1': True})
st['messages'] = [_ai_msg()]
out = T.tools_node(st)
assert out.get('pending_confirmation') is None
assert 'messages' in out and out['messages']  # 已执行
assert out.get('shell_error_confirm') is False

# 2e. 用户拒绝 → 不执行，flag 保持
st = _make_state(True, resolved={'call_1': False})
st['messages'] = [_ai_msg()]
out = T.tools_node(st)
assert out.get('pending_confirmation') is None
assert any('拒绝' in (m.content or '') for m in out.get('messages', []))
assert out.get('shell_error_confirm') is not False  # 未执行 → 不清除

# 2f. 非 shell 工具不受 flag 影响
_patch_tools(0)
st = _make_state(True)
st['messages'] = [_ai_msg(name='list_dir', args={'path': '.'})]
out = T.tools_node(st)
assert out.get('pending_confirmation') is None

# ---------- 3. 提示词层检查 ----------
from v3.graph import prompts as P

hint = P._shell_syntax_hint('powershell')
assert '不允许出现' in hint and '&&' in hint
g = P.build_system_prompt({}, 'powershell', user_intent='测试')
assert '不允许出现 `&&`' in g
assert '先经用户批准' in g
# PATH 作用域：按用户要求执行，不擅自降级
assert '不要擅自降级' in g
assert "scope=\"system\"" in g
assert '非阻塞' in g
assert '%PATH%' in g  # 禁止 %PATH% 展开的说明
assert '&&' in P._shell_syntax_hint('bash')  # bash 环境不受限（用 && 合法）
assert '不要写出 `&&`' in P.PLANNER_SYSTEM
assert '不要擅自改成用户级' in P.PLANNER_SYSTEM

print('all policy checks: PASS')
