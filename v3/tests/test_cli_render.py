from v3.tools.shell_tool import _powershell_syntax_guard as g

# && 拦截
assert g('cd D:/tmp && python hello.py') is not None
assert '不支持' in g('cd D:/tmp && python hello.py')
# 字符串里的 && 不算
assert g('bash -lc "cmd1 && cmd2"') is None
assert g("echo 'a && b'") is None
# || 拦截 / 2>nul / echo.
assert g('foo || bar') is not None
assert g('dir 2>nul') is not None
assert g('echo. hello') is not None
# 正常命令放行
assert g('Get-ChildItem -Force') is None
assert g('cd D:/tmp; python hello.py') is None
assert g('New-Item -Path a.txt -Force | Out-Null') is None
assert g('a > b.txt') is None
assert g('x 2> err.txt') is None  # PS 合法重定向
# cd /d（cmd 专有）
assert g('cd /d "D:\\PYTHON"') is not None
assert g('foo; cd /d C:\\') is not None
assert g('cd D:\\PYTHON') is None  # PS 原生跨盘
# 嵌套 shell 拦截
assert g('powershell -Command "Get-Location"') is not None
assert g('cmd /c dir') is not None
assert g('pwsh -c ls') is not None
assert g('Get-Location') is None
print('syntax guard: ALL PASS')

# 环境上下文模块
from v3.graph.env_context import env_context_for_planner, build_env_context_text

text = env_context_for_planner()
print('--- env context ---')
print(text)
print('-------------------')
assert '操作系统' in text
assert '工作目录' in text
assert '目录概览' in text
assert '关键环境变量' in text
assert '规划前必读' in text
# 目录概览里应能识别出启动器（当前 v3 仓库根有 v3.bat）
assert 'v3.bat' in text and '启动器' in text
print('env context: PASS')

# render_plan_step 双前缀修复
from v3.cli import renderer as R
assert '[step-1]' in R.render_plan_step('step-1', '描述', 'running', None, False)
assert '[step-2]' in R.render_plan_step(2, '描述', 'running', None, False)
print('step id fix: PASS')

# render_plan_confirm 新格式：只显示一遍 + 概述 + 风险
payload = {
    "kind": "plan_confirm",
    "plan": [
        {"description": "创建新技能 create_and_run_python_file", "status": "pending"},
        {"description": "写入代码内容", "status": "pending"},
    ],
    "summary": "创建并保存一个新技能",
    "risks": ["会写入技能库文件", "无破坏性操作"],
    "high_risk": False,
}
text = R.render_plan_confirm(payload, use_color=False)
assert '已生成方案' in text
assert '[step-1]' in text and '[step-2]' in text
assert '概述' in text and '创建并保存一个新技能' in text
assert '风险' in text and '会写入技能库文件' in text
# 高风险版本
payload["high_risk"] = True
text2 = R.render_plan_confirm(payload, use_color=False)
assert '⚠ 高风险' in text2
print('plan_confirm render: PASS')
