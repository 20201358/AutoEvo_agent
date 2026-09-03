import os
from openai import OpenAI

client = OpenAI(
  api_key= "sk-ws-H.EXRLRRI.BsTB.MEUCIGjdJsc0P77bUGEprqmlDAMBF2fCyT46EWgMrlJgdRmCAiEArv1bZ7KWxnjJwaLKy33PRJPE835JPV4Pd1slk9T9Dpo",
  base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

messages = [
    {"role": "system", "content": "你是一个乐于助人的AI助手。"}
]

def ask_agent(user_input):
    """向 Agent 提问并返回回复，同时打印 Token 消耗"""
    # 将用户消息加入历史
    messages.append({"role": "user", "content": user_input})
    
    try:
        response = client.chat.completions.create(
            model="qwen3.8-max",
            messages=messages,
            temperature=0.7,
        )
        
        # 提取回复
        reply = response.choices[0].message.content
        # 提取 Token 用量
        usage = response.usage
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        total_tokens = usage.total_tokens
        
        # 将助手回复也加入历史，以便多轮对话
        messages.append({"role": "assistant", "content": reply})
        
        # 打印结果
        print("\n🤖 Agent 回复：")
        print(reply)
        print("\n📊 Token 消耗：")
        print(f"   - 输入 Token: {prompt_tokens}")
        print(f"   - 输出 Token: {completion_tokens}")
        print(f"   - 总计 Token: {total_tokens}")
        print("-" * 50)
        
        return reply
        
    except Exception as e:
        print(f"❌ 请求失败：{e}")
        return None

def main():
    print("🚀 交互式 Agent 已启动（输入 exit 或 quit 退出）")
    print("=" * 50)
    while True:
        user_input = input("\n👤 你: ").strip()
        if user_input.lower() in ["exit", "quit"]:
            print("👋 再见！")
            break
        if not user_input:
            continue
        ask_agent(user_input)

if __name__ == "__main__":
    main()