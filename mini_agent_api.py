"""
mini_agent_api.py
A 50‑line LangChain ReAct agent that remembers the last ~4 K tokens,
answers one message per request, and exposes a single /chat endpoint.

Start it with:
    python mini_agent_api.py
(Use CTRL+C to stop.)

——————————————————————————————————————————
If you want live‑reload during development, skip the __main__ block
and instead run:
    uvicorn mini_agent_api:app --reload
——————————————————————————————————————————
"""

from fastapi import FastAPI
from pydantic import BaseModel
from langchain_openai import ChatOpenAI          # DeepSeek uses OpenAI‑style API
from langchain.memory import ConversationBufferWindowMemory
from langchain.agents import initialize_agent, AgentType
import requests, json
from langchain.tools import Tool

# ───── LLM configuration ────────────────────────────────────────────────────
GPT_API_KEY      = "sk-729db63993834e39a41361de89cf7d41"
GPT_BASE_URL     = "https://api.deepseek.com"
GPT_MODEL_ENGINE = "deepseek-chat"

llm = ChatOpenAI(
    model=GPT_MODEL_ENGINE,
    base_url=GPT_BASE_URL,
    api_key=GPT_API_KEY,
    streaming=True
)

memory = ConversationBufferWindowMemory(
    k=6,                        # keep last 6 turns
    memory_key="chat_history",
    return_messages=True
)

def call_mcp_tool(tool_url: str, payload: dict) -> str:
    """
    Generic POST helper.  Expects the MCP server to return
    {'type':'text','text': '...'}  or  {'type':'image','url':'...'}
    in its SSE/REST response.  Adapt if your schema differs.
    """
    try:
        r = requests.post(tool_url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("type") == "text":
            return data["text"]
        elif data.get("type") == "image":
            return f"[图片已生成] {data['url']}"
        else:
            return str(data)
    except Exception as exc:
        return f"调用 MCP 工具失败: {exc}"

# 3‑a  MongoDB query tool
def mongo_query(question: str) -> str:
    return call_mcp_tool(
        "http://localhost:3001/mongo/query",
        {"question": question}
    )

mongo_tool = Tool(
    name="mongo_med_qa",
    func=mongo_query,
    description=(
        "回答医疗文件相关问题时使用。"
        " 输入应是自然语言问题，例如“患者 2023‑05‑14 的肝功能结果？”。"
    )
)

# 3‑b  Weather‑chart tool (for demonstration)
def weather_chart(city: str) -> str:
    return call_mcp_tool(
        "http://localhost:3001/weather_chart",
        {"city": city}
    )

weather_tool = Tool(
    name="weather_chart",
    func=weather_chart,
    description=(
        "生成过去 5 天的天气折线图并返回图片 URL。"
        " 输入是城市名，例如“Shanghai”。"
    )
)
TOOLS = [mongo_tool, weather_tool]  # add more as you wrap them
agent = initialize_agent(
    tools=TOOLS,
    llm=llm,
    agent=AgentType.CHAT_ZERO_SHOT_REACT_DESCRIPTION,
    memory=memory,
    verbose=False,
)
# ───── FastAPI surface ──────────────────────────────────────────────────────
app = FastAPI(title="Mini ReAct Agent")

class ChatReq(BaseModel):
    message: str
    username: str = "User"

class ChatResp(BaseModel):
    answer: str

@app.post("/chat", response_model=ChatResp)
async def chat(req: ChatReq):
    """Return a one‑shot answer while the memory keeps rolling context."""
    answer = agent.run(req.message)
    return {"answer": answer}

# ───── Manual launch ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)   # ← no reload flag
