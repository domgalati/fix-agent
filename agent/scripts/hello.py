from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage

SYSTEM = """You are a FIX protocol support engineer. Given a single FIX message,
identify: (1) the MsgType, (2) whether it indicates a session-level or
application-level concern, (3) one specific thing to check next.
Be terse. No preamble."""

FIX_LINE = (
    "8=FIX.4.4|9=112|35=5|34=9|49=SENDER|52=20260423-14:22:17.123|"
    "56=TARGET|58=MsgSeqNum too low, expecting 15 but received 9|10=201|"
)

llm = ChatOllama(
    model="qwen2.5:7b-instruct-q4_K_M",
    base_url="http://192.168.0.85:11434",
    temperature=0,
    num_ctx=8192,
)

response = llm.invoke([
    SystemMessage(content=SYSTEM),
    HumanMessage(content=f"Analyze this FIX message:\n{FIX_LINE}"),
])

print(response.content)
