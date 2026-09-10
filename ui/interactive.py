"""交互式界面（高级）"""
from ui.cli import CLI
from tools.command_executor import CommandExecutor
from tools.security_checker import SecurityChecker


class InteractiveCLI(CLI):
    """交互式命令行界面，支持命令实际执行"""
    
    def __init__(self, dry_run: bool = True):
        super().__init__()
        self.executor = CommandExecutor(dry_run=dry_run)
        self.dry_run = dry_run
        
    def _handle_special_commands(self, user_input: str) -> bool:
        """处理特殊命令（扩展）"""
        if super()._handle_special_commands(user_input):
            return True
        
        # 执行命令
        if user_input == "执行" and self.current_command:
            self._execute_current_command()
            return True
        
        # 修改命令
        if user_input.startswith("修改 ") and self.current_command:
            self._modify_command(user_input[3:])
            return True
        
        return False
    
    def _execute_current_command(self):
        """执行当前命令"""
        print(f"\n🚀 执行: {self.current_command}")
        
        # 执行前安全检查
        safety_check = SecurityChecker.check_command_safety(self.current_command)
        if safety_check["risk_level"] == "high":
            confirm = input("⚠️ 这是危险命令，确认执行? (y/n): ")
            if confirm.lower() != 'y':
                print("❌ 已取消执行")
                return
        
        result = self.executor.execute(
            self.current_command,
            confirm=False
        )
        
        if result["success"]:
            print(f"✅ 执行成功")
            if result["output"]:
                print(result["output"])
        else:
            print(f"❌ 执行失败: {result['error']}")
        
        if result["dry_run"]:
            print("ℹ️ 这是模拟执行，未实际运行命令")
    
    def _modify_command(self, feedback: str):
        """修改命令"""
        self.state["command"] = self.current_command
        self.state["messages"].append({
            "role": "user",
            "content": feedback
        })
        self.state["next_step"] = "modify"
        
        try:
            result = self.agent.invoke(self.state, self.config)
            self.state = result
            
            if result.get("command"):
                self.current_command = result["command"]
                print(f"\n🔄 修改后的命令: {self.current_command}")
        except Exception as e:
            print(f"\n❌ 修改失败: {e}")