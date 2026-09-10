"""安全检查模块"""
from typing import Dict, List, Tuple
from config.settings import settings


class SecurityChecker:
    """安全检查器"""
    
    @staticmethod
    def check_command_safety(command: str) -> Dict:
        """检查命令安全性"""
        is_danger, reason = SecurityChecker._check_dangerous(command)
        
        result = {
            "safe": not is_danger,
            "risk_level": "low",
            "warnings": [],
            "suggestions": []
        }
        
        if is_danger:
            result["risk_level"] = "high"
            result["warnings"].append(reason)
            result["suggestions"].append("请确认此操作的必要性")
        
        # 检查 sudo 命令
        if "sudo" in command:
            result["risk_level"] = "high"
            result["warnings"].append("使用 sudo 执行，需要管理员权限")
        
        # 检查递归操作
        if "-r" in command or "-R" in command or "recursive" in command:
            if "rm" in command or "chmod" in command or "chown" in command:
                result["risk_level"] = "high"
                result["warnings"].append("递归操作可能影响大量文件")
        
        # 检查通配符
        if "*" in command and ("rm" in command or "mv" in command or "cp" in command):
            result["warnings"].append("使用通配符时请注意匹配范围")
        
        return result
    
    @staticmethod
    def _check_dangerous(command: str) -> Tuple[bool, str]:
        """检查是否包含危险关键词"""
        for dangerous in settings.DANGEROUS_COMMANDS:
            if dangerous in command:
                return True, f"检测到危险命令关键词: {dangerous}"
        return False, ""
    
    @staticmethod
    def suggest_safer_alternative(command: str) -> List[str]:
        """建议更安全的替代命令"""
        suggestions = []
        
        # rm 命令建议
        if "rm" in command and "-rf" in command:
            suggestions.append("考虑使用 'rm -i' 交互式删除")
            suggestions.append("或者先使用 'ls' 确认要删除的文件")
        
        # chmod 命令建议
        if "chmod 777" in command:
            suggestions.append("使用 'chmod 755' 可能更安全")
            suggestions.append("避免使用 777 权限")
        
        # sudo 命令建议
        if "sudo" in command:
            suggestions.append("确认是否真的需要 sudo 权限")
        
        return suggestions