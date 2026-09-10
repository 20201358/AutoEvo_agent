"""Prompt 模板定义"""
"""Prompt 模板定义"""
from langchain_core.prompts import ChatPromptTemplate


class PromptTemplates:
    """所有 Prompt 模板 - 使用 @staticmethod"""
    
    @staticmethod
    def understand_request():
        """理解用户请求的模板"""
        return ChatPromptTemplate.from_messages([
            ("system", """你是一个终端命令专家。根据用户的自然语言描述，理解他们想要执行的操作。
            
请输出JSON格式，包含以下字段：
{{
    "command": "完整的终端命令",
    "explanation": "命令的详细解释和参数说明",
    "risk_level": "low|medium|high"
}}

风险等级判断标准：
- low: 查询类命令 (ls, cat, grep, find 等)
- medium: 修改配置类 (chmod, chown, mv, cp 等)
- high: 危险操作 (rm -rf, sudo, dd, mkfs, format 等)

注意：
1. 对于危险命令，需要添加警告信息
2. 考虑操作系统的兼容性
3. 提供完整的命令路径
"""),
            ("user", "{request}")
        ])
    
    @staticmethod
    def confirm_command():
        """确认命令的模板"""
        return ChatPromptTemplate.from_messages([
            ("system", """根据用户的历史对话，判断命令是否需要进一步处理。

判断标准：
1. **execute** - 以下情况选择执行：
   - 命令完全符合用户需求
   - 用户明确表示满意
   - 已经修改过 2 次以上
   - 命令清晰且无歧义

2. **modify** - 以下情况选择修改：
   - 用户提出了具体的修改要求
   - 命令缺少关键参数
   - 有更优的实现方式（但不超过2次修改）

3. **clarify** - 以下情况选择澄清：
   - 用户需求不明确
   - 命令有多个可能的解释
   - 缺少关键信息（如文件路径、参数等）

注意：避免无休止的修改循环，如果已经修改过多次，建议执行。
"""),
            ("user", """用户原始请求: {request}
当前命令: {command}
风险等级: {risk_level}
已修改次数: {modify_count}
历史对话: {messages}

请返回决策词 (execute/modify/clarify)：""")
        ])
    
    @staticmethod
    def modify_command():
        """修改命令的模板"""
        return ChatPromptTemplate.from_messages([
            ("system", """你是一个终端命令专家。根据用户的反馈修改命令。

修改规则：
1. **一次只做一个修改**：根据用户反馈进行最小化修改
2. **完整命令**：输出完整的修改后命令
3. **清晰说明**：用注释或说明解释修改了什么
4. **限制修改次数**：如果已经修改 {modify_count} 次，考虑直接给出最终版本

如果用户反馈是：
- "按大小排序" → 在原有命令上添加排序参数
- "显示详细信息" → 添加 -l 或 -v 参数
- "改为递归" → 添加 -r 或 -R 参数
- "修改路径" → 更改文件路径

输出要求：
- 只输出修改后的完整命令
- 如果需要多个命令，用 && 或 ; 连接
- 确保命令在目标系统上可用
"""),
            ("user", """原始命令: {command}
命令说明: {explanation}
用户反馈: {feedback}
已修改次数: {modify_count}

请输出修改后的完整命令：""")
        ])
    
    @staticmethod
    def clarify_request():
        """澄清请求的模板"""
        return ChatPromptTemplate.from_messages([
            ("system", """根据用户的不明确请求，生成一个清晰的问题来获取更多信息。

提问策略：
1. 具体化：询问具体的目标文件、目录、参数
2. 选项化：提供几个可能的选项让用户选择
3. 示例化：给出示例说明期望的输入格式
"""),
            ("user", """用户请求: {request}
当前生成的命令: {command}
上下文: {context}

请生成澄清问题：""")
        ])
    
    @staticmethod
    def analyze_command():
        """分析命令的模板"""
        return ChatPromptTemplate.from_messages([
            ("system", """分析终端命令的安全性、作用和潜在影响。

输出JSON格式：
{{
    "safe": true/false,
    "risk_level": "low|medium|high",
    "warnings": ["警告1", "警告2"],
    "alternatives": ["替代命令1", "替代命令2"],
    "description": "命令的详细描述"
}}
"""),
            ("user", "命令: {command}")
        ])
    
    @staticmethod
    def generate_alternative():
        """生成替代命令的模板"""
        return ChatPromptTemplate.from_messages([
            ("system", """为用户提供更安全或更合适的替代命令。

考虑：
1. 安全性：是否有更安全的实现方式
2. 效率：是否有更高效的命令
3. 可读性：是否更易于理解
"""),
            ("user", """原始命令: {command}
风险等级: {risk_level}
问题: {issue}
请提供替代方案：""")
        ])