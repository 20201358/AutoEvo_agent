"""命令执行器（模拟和实际执行）"""
import subprocess
import shlex
from typing import Dict, Optional, List
from tools.security_checker import SecurityChecker


class CommandExecutor:
    """命令执行器"""
    
    def __init__(self, dry_run: bool = True):
        """
        Args:
            dry_run: True 表示模拟执行，False 表示实际执行
        """
        self.dry_run = dry_run
        self.history: List[Dict] = []
    
    def execute(self, command: str, confirm: bool = False) -> Dict:
        """
        执行命令
        
        Returns:
            {
                "success": bool,
                "output": str,
                "error": str,
                "command": str,
                "dry_run": bool
            }
        """
        # 安全检查
        safety_check = SecurityChecker.check_command_safety(command)
        
        result = {
            "success": False,
            "output": "",
            "error": "",
            "command": command,
            "dry_run": self.dry_run,
            "safety_check": safety_check
        }
        
        # 如果命令不安全且未确认
        if not safety_check["safe"] and not confirm:
            result["error"] = "命令被安全策略阻止，需要确认执行"
            return result
        
        # 模拟执行
        if self.dry_run:
            result["success"] = True
            result["output"] = f"[模拟执行] {command}"
            self.history.append(result)
            return result
        
        # 实际执行
        try:
            # 分割命令
            args = shlex.split(command)
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                shell=False
            )
            result["success"] = proc.returncode == 0
            result["output"] = proc.stdout
            result["error"] = proc.stderr
            self.history.append(result)
        except Exception as e:
            result["error"] = str(e)
        
        return result
    
    def get_history(self) -> List[Dict]:
        """获取命令执行历史"""
        return self.history
    
    def clear_history(self):
        """清空历史"""
        self.history.clear()