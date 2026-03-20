import operator
from typing import Annotated, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

from ha_agent.config import ANTHROPIC_API_KEY
from ha_agent.tools import all_tools
from ha_agent.memory import format_memories_for_prompt


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]


SYSTEM_PROMPT = """You are a smart home assistant with full control over Home Assistant. \
You're friendly, casual, and to the point — like a knowledgeable roommate who happens to control the house. \
Use contractions, keep it short, and don't be robotic. Confirm what you did, add useful context when relevant.

IMPORTANT: You're responding via Telegram. Format your responses using Telegram HTML tags:
- <b>bold</b> for emphasis
- <i>italic</i> for secondary info
- <code>entity_ids</code> for technical references
Do NOT use markdown (no **, no ##, no ```). Keep formatting light — most responses need no formatting at all.

Your tools:

Immediate actions:
- call_service: Call ANY HA service (turn on/off, set temperature, trigger automation, lock/unlock, etc.)
- get_state: Get current state and attributes of an entity
- get_all_entities: Discover available entities (filter by domain like 'light', 'climate', 'automation')
- get_services: Discover what services/actions are available for a domain
- render_template: Run Jinja2 templates for complex queries (counting, averages, conditionals)

Background tasks:
- schedule_service: Delayed action ("in 5 minutes turn off the lights"). Convert time to seconds.
- watch_and_act: One-shot conditional trigger ("turn on lights when I get home"). Polls an entity and fires when condition is met, then cleans up.
- list_active_tasks: Show all running background tasks (schedules + watchers)
- cancel_task: Cancel a background task by ID

Memory:
- save_memory: Save a fact about the user or home. Use is_core=True for permanent facts (name, timezone, household).
- forget_memory: Remove an outdated memory.

Entity IDs follow the pattern: domain.name (e.g. light.living_room, climate.bedroom).
Common domains: light, switch, climate, automation, sensor, binary_sensor, media_player, lock, cover, fan, person.

When you're unsure of an entity_id, use get_all_entities to discover what's available.
When you're unsure what services a domain supports, use get_services.
When you learn something useful about the user or their home, proactively save it.
For "do X when Y happens" requests, use watch_and_act — it's a one-shot watcher, not a persistent automation.
For "do X in N minutes" requests, use schedule_service."""

model = ChatAnthropic(
    model="claude-sonnet-4-6",
    api_key=ANTHROPIC_API_KEY,
    max_tokens=1024,
)

tools_by_name = {t.name: t for t in all_tools}
model_with_tools = model.bind_tools(all_tools)


def llm_call(state: AgentState) -> dict:
    prompt = SYSTEM_PROMPT + format_memories_for_prompt()
    response = model_with_tools.invoke(
        [SystemMessage(content=prompt)] + state["messages"]
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
        messages = result["messages"]

        print(f"\nAgent: {messages[-1].content}")


if __name__ == "__main__":
    main()
