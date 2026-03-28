import logging
import operator
from typing import Annotated, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

from ha_agent.config import ANTHROPIC_API_KEY
from ha_agent.tools import all_tools
from ha_agent.memory import format_memories_for_prompt

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]


# Tool descriptions live in tool schemas (bind_tools) — don't duplicate here.
SYSTEM_PROMPT = """\
You are a smart home assistant controlling Home Assistant. \
Friendly, casual, brief — like a knowledgeable roommate.

Respond via Telegram using HTML only: <b>bold</b>, <i>italic</i>, <code>entity_ids</code>. No markdown.

Entity IDs: domain.name (e.g. light.living_room, climate.bedroom).
When unsure of an entity_id, use get_all_entities. When unsure of services, use get_services.
For "do X when Y" → watch_and_act. For "do X in N minutes" → schedule_service.
Proactively save_memory when you learn useful facts."""

MAX_HISTORY = 20  # max messages sent to the LLM per call

model = ChatAnthropic(
    model="claude-sonnet-4-6",
    api_key=ANTHROPIC_API_KEY,
    max_tokens=1024,
)

tools_by_name = {t.name: t for t in all_tools}
model_with_tools = model.bind_tools(all_tools)


def _trim_history(messages: list, keep: int = MAX_HISTORY) -> list:
    """Keep last `keep` messages, preserving tool-call/result pairs."""
    if len(messages) <= keep:
        return messages
    trimmed = messages[-keep:]
    # Don't start on a ToolMessage — walk back to include its AIMessage
    while trimmed and isinstance(trimmed[0], ToolMessage):
        cut_idx = len(messages) - len(trimmed) - 1
        if cut_idx >= 0:
            trimmed.insert(0, messages[cut_idx])
        else:
            break
    return trimmed


def llm_call(state: AgentState) -> dict:
    prompt = SYSTEM_PROMPT + format_memories_for_prompt()
    messages = _trim_history(state["messages"])
    response = model_with_tools.invoke(
        [SystemMessage(content=prompt)] + messages
    )
    usage = response.response_metadata.get("usage", {})
    if usage:
        logger.info(
            "Tokens — in: %s, out: %s, cache_read: %s",
            usage.get("input_tokens", "?"),
            usage.get("output_tokens", "?"),
            usage.get("cache_read_input_tokens", 0),
        )
    return {"messages": [response]}


def tool_node(state: AgentState) -> dict:
    results = []
    for tool_call in state["messages"][-1].tool_calls:
        tool_fn = tools_by_name[tool_call["name"]]
        observation = tool_fn.invoke(tool_call["args"])
        results.append(
            ToolMessage(content=str(observation), tool_call_id=tool_call["id"])
        )
    return {"messages": results}


def should_continue(state: AgentState) -> Literal["tool_node", "__end__"]:
    if state["messages"][-1].tool_calls:
        return "tool_node"
    return "__end__"


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("llm_call", llm_call)
    builder.add_node("tool_node", tool_node)
    builder.add_edge(START, "llm_call")
    builder.add_conditional_edges("llm_call", should_continue, ["tool_node", "__end__"])
    builder.add_edge("tool_node", "llm_call")
    return builder.compile()


def main():
    graph = build_graph()
    messages = []

    print("Home Assistant Agent (type 'quit' to exit)")
    print("-" * 45)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        messages.append(HumanMessage(content=user_input))
        result = graph.invoke({"messages": messages})
        messages = result["messages"][-40:]

        print(f"\nAgent: {messages[-1].content}")


if __name__ == "__main__":
    main()
