import json
import threading
import contextvars
import time as _time
from langchain_core.tools import tool
from ha_agent.ha_client import ha
from ha_agent import memory


# --- Notification System ---
# Background tasks need to notify the user when they fire.
# In CLI mode: print(). In Telegram mode: send a message to the chat.
_notify_callback = None  # (chat_id: int, message: str) -> None
_current_chat_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_chat_id", default=None)


def set_notify_callback(fn):
    global _notify_callback
    _notify_callback = fn


def _notify(chat_id: int | None, message: str):
    if _notify_callback and chat_id:
        _notify_callback(chat_id, message)
    else:
        print(message)


# --- Task Registry ---
# Shared dict tracking all active background tasks (schedules + watchers)
_task_counter = 0
_task_lock = threading.Lock()
_active_tasks: dict[int, dict] = {}


def _register_task(description: str, kind: str, **extra) -> int:
    global _task_counter
    with _task_lock:
        _task_counter += 1
        task_id = _task_counter
        _active_tasks[task_id] = {
            "id": task_id,
            "kind": kind,
            "description": description,
            "created_at": _time.time(),
            **extra,
        }
    return task_id


def _remove_task(task_id: int):
    with _task_lock:
        _active_tasks.pop(task_id, None)


def _state_matches(current: str, target: str) -> bool:
    """Check if current state matches target — exact match or numeric comparison."""
    if current == target:
        return True
    try:
        return float(current) <= float(target)
    except (ValueError, TypeError):
        return False


# --- Core Tools ---

@tool
def call_service(domain: str, service: str, entity_id: str = "", data: str = "{}") -> str:
    """Call a Home Assistant service to control devices.

    Args:
        domain: Service domain (e.g. 'light', 'switch', 'climate').
        service: Service name (e.g. 'turn_on', 'turn_off', 'toggle').
        entity_id: Target entity. Empty if not needed.
        data: JSON service data. Defaults to '{}'.
    """
    try:
        parsed_data = json.loads(data) if data and data != "{}" else None
        eid = entity_id if entity_id else None
        result = ha.call_service(domain, service, entity_id=eid, data=parsed_data)
        return f"Service {domain}/{service} called successfully. {len(result)} entities affected."
    except Exception as e:
        return f"Error calling {domain}/{service}: {e}"


@tool
def get_state(entity_id: str) -> str:
    """Get current state and attributes of an entity.

    Args:
        entity_id: Entity to query (e.g. 'light.living_room').
    """
    try:
        state = ha.get_state(entity_id)
        attrs = state.get("attributes", {})
        lines = [
            f"Entity: {state['entity_id']}",
            f"State: {state['state']}",
            f"Last changed: {state['last_changed']}",
        ]
        if attrs.get("friendly_name"):
            lines.insert(1, f"Name: {attrs['friendly_name']}")
        # Cap attributes to avoid bloating context
        attr_items = [(k, v) for k, v in attrs.items() if k != "friendly_name"]
        for key, val in attr_items[:10]:
            lines.append(f"  {key}: {val}")
        if len(attr_items) > 10:
            lines.append(f"  ... and {len(attr_items) - 10} more attributes")
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting state of {entity_id}: {e}"


@tool
def get_all_entities(domain_filter: str = "") -> str:
    """List entities, optionally filtered by domain. Use domain_filter to narrow results.

    Args:
        domain_filter: Domain to filter by (e.g. 'light', 'climate'). Empty for all.
    """
    try:
        states = ha.get_all_states()
        if domain_filter:
            states = [s for s in states if s["entity_id"].startswith(f"{domain_filter}.")]
        lines = []
        for s in states:
            name = s.get("attributes", {}).get("friendly_name", "")
            lines.append(f"  {s['entity_id']} ({s['state']}){f' — {name}' if name else ''}")
        total = len(lines)
        # Cap output to avoid blowing up context
        if total > 50:
            lines = lines[:50]
            return f"Found {total} entities (showing first 50 — use domain_filter to narrow):\n" + "\n".join(lines)
        return f"Found {total} entities:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing entities: {e}"


@tool
def get_services(domain: str = "") -> str:
    """List available services for a domain.

    Args:
        domain: Domain to filter (e.g. 'light', 'climate'). Empty for all domains.
    """
    try:
        services = ha.get_services()
        if domain:
            for entry in services:
                if entry["domain"] == domain:
                    svc_names = list(entry["services"].keys())
                    return f"Services for '{domain}':\n  " + "\n  ".join(svc_names)
            return f"No services found for domain '{domain}'."
        lines = []
        for entry in services:
            count = len(entry["services"])
            lines.append(f"  {entry['domain']}: {count} services")
        return f"Available domains ({len(lines)}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing services: {e}"


@tool
def render_template(template: str) -> str:
    """Render a Jinja2 template in Home Assistant for complex queries.

    Args:
        template: Jinja2 template string.
    """
    try:
        return ha.render_template(template)
    except Exception as e:
        return f"Error rendering template: {e}"


# --- Background Task Tools ---

@tool
def schedule_service(delay_seconds: int, domain: str, service: str, entity_id: str = "", data: str = "{}") -> str:
    """Schedule a service call after a delay. Convert minutes to seconds.

    Args:
        delay_seconds: Seconds to wait (e.g. 300 for 5 minutes).
        domain: Service domain.
        service: Service name.
        entity_id: Target entity.
        data: JSON service data. Defaults to '{}'.
    """
    try:
        parsed_data = json.loads(data) if data and data != "{}" else None
        eid = entity_id if entity_id else None

        chat_id = _current_chat_id.get(None)

        mins, secs = divmod(delay_seconds, 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        task_id = _register_task(
            f"schedule: {domain}/{service} on {entity_id} in {time_str}",
            kind="schedule",
        )

        def _run():
            try:
                ha.call_service(domain, service, entity_id=eid, data=parsed_data)
                _notify(chat_id, f"[Task #{task_id}] Executed: {domain}/{service} on {entity_id}")
            except Exception as e:
                _notify(chat_id, f"[Task #{task_id}] Failed: {domain}/{service} on {entity_id} — {e}")
            finally:
                _remove_task(task_id)

        timer = threading.Timer(delay_seconds, _run)
        timer.daemon = True
        timer.start()

        return f"Task #{task_id}: Scheduled {domain}/{service} on {entity_id} in {time_str}."
    except Exception as e:
        return f"Error scheduling task: {e}"


@tool
def watch_and_act(
    watch_entity: str,
    target_state: str,
    then_domain: str,
    then_service: str,
    then_entity: str = "",
    then_data: str = "{}",
    poll_interval_seconds: int = 5,
    timeout_minutes: int = 60,
) -> str:
    """Watch an entity and fire a one-shot service call when it reaches a target state.

    Args:
        watch_entity: Entity to monitor.
        target_state: State to wait for (e.g. 'home', 'below_horizon', '60').
        then_domain: Service domain to call when matched.
        then_service: Service to call.
        then_entity: Target entity for the action.
        then_data: JSON service data. Defaults to '{}'.
        poll_interval_seconds: Check interval in seconds. Defaults to 5.
        timeout_minutes: Give up after N minutes. Defaults to 60.
    """
    try:
        parsed_data = json.loads(then_data) if then_data and then_data != "{}" else None
        then_eid = then_entity if then_entity else None
        chat_id = _current_chat_id.get(None)

        # Capture the current state so we detect a TRANSITION, not just current value.
        # If already in target state, we wait for it to leave and come back.
        try:
            initial_state = ha.get_state(watch_entity)["state"]
        except Exception:
            initial_state = None

        already_in_target = _state_matches(initial_state, target_state) if initial_state else False

        task_id = _register_task(
            f"watch: {watch_entity} → '{target_state}' → {then_domain}/{then_service} on {then_entity}"
            + (" (waiting for state to change first)" if already_in_target else ""),
            kind="watcher",
        )

        if already_in_target:
            _notify(chat_id,
                f"[Task #{task_id}] Note: {watch_entity} is already '{target_state}'. "
                f"Waiting for it to change away and back.")

        def _poll():
            prev_state = initial_state
            saw_different_state = not already_in_target

            deadline = _time.time() + (timeout_minutes * 60)
            while _time.time() < deadline:
                with _task_lock:
                    if task_id not in _active_tasks:
                        return

                try:
                    current = ha.get_state(watch_entity)["state"]

                    # Track if we've seen a non-target state (for transition detection)
                    if not saw_different_state:
                        if not _state_matches(current, target_state):
                            saw_different_state = True

                    # Only fire if we've seen a transition TO the target state
                    if saw_different_state and _state_matches(current, target_state):
                        try:
                            ha.call_service(then_domain, then_service, entity_id=then_eid, data=parsed_data)
                            _notify(chat_id, f"[Task #{task_id}] Triggered! {watch_entity} changed to '{target_state}' → {then_domain}/{then_service} on {then_entity}")
                        except Exception as e:
                            _notify(chat_id, f"[Task #{task_id}] Condition met but action failed: {e}")
                        finally:
                            _remove_task(task_id)
                        return

                    prev_state = current
                except Exception:
                    pass  # Transient API errors — keep polling

                _time.sleep(poll_interval_seconds)

            _notify(chat_id, f"[Task #{task_id}] Timed out after {timeout_minutes}m waiting for {watch_entity} → '{target_state}'")
            _remove_task(task_id)

        thread = threading.Thread(target=_poll, daemon=True)
        thread.start()

        return (
            f"Task #{task_id}: Watching {watch_entity} for state '{target_state}'. "
            f"When matched → {then_domain}/{then_service} on {then_entity}. "
            f"Polling every {poll_interval_seconds}s, timeout {timeout_minutes}m."
        )
    except Exception as e:
        return f"Error setting up watcher: {e}"


@tool
def list_active_tasks() -> str:
    """List all active background tasks (schedules and watchers)."""
    with _task_lock:
        if not _active_tasks:
            return "No active background tasks."
        lines = []
        now = _time.time()
        for task in _active_tasks.values():
            elapsed = int(now - task["created_at"])
            mins, secs = divmod(elapsed, 60)
            lines.append(f"  #{task['id']} [{task['kind']}] {task['description']} (running {mins}m {secs}s)")
        return f"Active tasks ({len(lines)}):\n" + "\n".join(lines)


@tool
def cancel_task(task_id: int) -> str:
    """Cancel a background task by ID.

    Args:
        task_id: Task ID to cancel.
    """
    with _task_lock:
        if task_id in _active_tasks:
            task = _active_tasks.pop(task_id)
            return f"Cancelled task #{task_id}: {task['description']}"
        return f"No active task with ID #{task_id}."


# --- Memory Tools ---

@tool
def save_memory(fact: str, is_core: bool = False) -> str:
    """Save a fact about the user or home. Core=permanent (name, timezone), learned=general.

    Args:
        fact: Concise fact to remember.
        is_core: True for permanent facts, False for general.
    """
    return memory.add_memory(fact, is_core=is_core)


@tool
def forget_memory(fact: str) -> str:
    """Remove a saved memory.

    Args:
        fact: Exact fact to forget.
    """
    return memory.forget_memory(fact)


all_tools = [
    call_service, get_state, get_all_entities, get_services, render_template,
    schedule_service, watch_and_act, list_active_tasks, cancel_task,
    save_memory, forget_memory,
]
