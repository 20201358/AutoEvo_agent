"""命令解析工具"""
import re
import json
from typing import Dict, List, Optional, Tuple


class CommandParser:
    """命令解析器"""
    
    @staticmethod
    def parse_command(command: str) -> Dict:
        """解析命令的结构"""
        parts = command.strip().split()
        if not parts:
            return {}
        
        return {
            "command": parts[0],
            "args": parts[1:],
            "full_command": command
        }
    
    @staticmethod
    def extract_parameters(command: str) -> Dict[str, str]:
        """提取命令参数"""
        params = {}
        # 匹配 -参数 或 --参数 格式
        pattern = r'(-{1,2}[a-zA-Z0-9-]+)(?:\s+([^-][^\s]*))?'
        matches = re.findall(pattern, command)
        for match in matches:
            key = match[0]
            value = match[1] if len(match) > 1 else ""
            params[key] = value
        return params
    
    @staticmethod
    def is_dangerous(command: str, dangerous_list: List[str]) -> Tuple[bool, str]:
        """检查命令是否危险"""
        for dangerous in dangerous_list:
            if dangerous in command:
                return True, f"包含危险关键词: {dangerous}"
        return False, ""

    @staticmethod
    def get_command_family(command: str) -> str:
        """获取命令家族"""
        cmd = command.strip().split()[0]
        families = {
            "file": ["ls", "cat", "grep", "find", "touch", "rm", "cp", "mv", "mkdir", "chmod", "chown"],
            "process": ["ps", "kill", "top", "htop", "pgrep", "pkill"],
            "network": ["ping", "curl", "wget", "ssh", "scp", "netstat", "ss"],
            "system": ["sudo", "systemctl", "service", "df", "du", "free", "uname"],
            "package": ["apt", "yum", "pip", "npm", "brew", "docker"],
        }
        for family, commands in families.items():
            if cmd in commands:
                return family
        return "unknown"