import os
from dotenv import load_dotenv
from typing import Optional

load_dotenv(os.path.join(os.path.dirname(__file__),'..','.env'))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")

# 检查API Key是否加载成功
if not OPENAI_API_KEY:
    raise ValueError("请在 .env 文件中设置 OPENAI_API_KEY")

#可进行相关检查

class Settings:
    """应用配置"""
    
    # OpenAI 配置
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL: Optional[str] = os.getenv("OPENAI_MODEL")
    OPENAI_TEMPERATURE: float = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))
    OPENAI_BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL")
    
    # 应用配置
    APP_NAME: str = "Terminal Assistant"
    APP_VERSION: str = "1.0.0"
    
    # 安全配置
    DANGEROUS_COMMANDS: list = [
        "rm -rf", "sudo", "dd", "mkfs", "format",
        "chmod 777", "chown", "kill -9"
    ]
    
    @classmethod
    def get_llm_config(cls, temperature: Optional[float] = None):
        """获取 LLM 配置"""
        return {
            "model": cls.OPENAI_MODEL,
            "temperature": temperature or cls.OPENAI_TEMPERATURE,
            "openai_api_key": cls.OPENAI_API_KEY,
        }


settings = Settings()