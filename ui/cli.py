"""命令行界面"""
from typing import Optional
from core.state import create_initial_state
from core.graph import create_agent


class CLI:
    """命令行界面"""
    
    def __init__(self):
        self.agent = create_agent()
        self.config = {"configurable": {"thread_id": "1"}}
        self.state = create_initial_state()
        self.current_command = None
        
    def run(self):
        """运行界面"""
        self._show_header()
        
        while True:
            user_input = input("\n💬 你: ").strip()
            
            if user_input.lower() in ["exit", "quit"]:
                print("👋 再见！")
                break
                
            if user_input.lower() == "help":
                self._show_help()
                continue
            
            # 处理特殊命令
            if self._handle_special_commands(user_input):
                continue
            
            # 正常处理
            self._process_request(user_input)
    
    def _show_header(self):
        """显示标题"""
        print("=" * 60)
        print("🤖 终端命令 AI 助手")
        print("=" * 60)
        print("输入 'exit' 或 'quit' 退出程序")
        print("输入 'help' 查看帮助")
        print("-" * 60)
    
    def _show_help(self):
        """显示帮助"""
        print("""
可用命令:
  exit/quit   - 退出程序
  help        - 显示帮助
  history     - 查看命令历史
  clear       - 清屏
  
使用示例:
  "列出当前目录所有文件"
  "查找所有大于100MB的日志文件"
  "压缩当前目录为 backup.zip"
        """)
    
    def _handle_special_commands(self, user_input: str) -> bool:
        """处理特殊命令"""
        if user_input.lower() == "history":
            # 显示历史
            return True
        
        if user_input.lower() == "clear":
            import os
            os.system('cls' if os.name == 'nt' else 'clear')
            self._show_header()
            return True
        
        return False
    
    def _process_request(self, user_input: str):
        """处理用户请求"""
        self.state["user_request"] = user_input
        self.state["messages"].append({
            "role": "user",
            "content": user_input
        })
        
        try:
            result = self.agent.invoke(self.state, self.config)
            self.state = result
            
            # 显示生成的命令
            if result.get("command"):
                print(f"\n📝 命令: {result['command']}")
                if result.get("command_explanation"):
                    print(f"📖 说明: {result['command_explanation']}")
                self.current_command = result["command"]
            
            # 显示助手消息
            for msg in result.get("messages", []):
                if msg.get("role") == "assistant":
                    print(f"\n🤖 助手: {msg['content']}")
            
            # 显示操作选项
            if result.get("next_step") == "end" and result.get("command"):
                self._show_options()
                
        except Exception as e:
            print(f"\n❌ 错误: {e}")
    
    def _show_options(self):
        """显示操作选项"""
        print("\n💡 选项:")
        print("  - 输入 '执行' 运行命令")
        print("  - 输入 '修改 [要求]' 调整命令")
        print("  - 输入新描述生成其他命令")