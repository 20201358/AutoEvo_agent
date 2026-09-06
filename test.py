import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

# ✅ 加载 .env 文件
load_dotenv()

# 从环境变量获取配置
ZHIPU_API_KEY = os.getenv("OPENAI_API_KEY")
ZHIPU_BASE_URL = os.getenv("OPENAI_BASE_URL")

# 检查API Key是否加载成功
if not ZHIPU_API_KEY:
    raise ValueError("请在 .env 文件中设置 ZHIPU_API_KEY")

# 初始化LLM
llm = ChatOpenAI(
    base_url=ZHIPU_BASE_URL,
    api_key=ZHIPU_API_KEY,
    model="glm-4.5-air",  # 使用你的12M tokens额度
    temperature=0.7,
)

# 测试调用
def test_llm():
    response = llm.invoke("你好，介绍一下你自己")
    print(response.content)

if __name__ == "__main__":
    test_llm()