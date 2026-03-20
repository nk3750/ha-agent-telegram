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
    """Call any Home Assistant service. This is the primary tool for controlling devices.

    Args:
        domain: The service domain, e.g. 'light', 'switch', 'climate', 'automation',
                'media_player', 'lock', 'cover', 'fan', 'homeassistant'.
        service: The service to call, e.g. 'turn_on', 'turn_off', 'toggle',
                 'set_temperature', 'trigger', 'lock', 'unlock', 'open_cover', 'close_cover'.
        entity_id: The target entity, e.g. 'light.living_room', 'climate.bedroom'.
                   Leave empty for services that don't need an entity.
        data: JSON string of additional service data, e.g. '{"brightness": 200}' or
              '{"temperature": 72}'. Defaults to '{}'.
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
    """Get the current state and attributes of a Home Assistant entity.

    Args:
        entity_id: The entity to query, e.g. 'light.living_room', 'climate.bedroom',
                   'sensor.temperature', 'automation.morning_routine'.
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
        for key, val in attrs.items():
            if key != "friendly_name":
                lines.append(f"  {key}: {val}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting state of {entity_id}: {e}"


@tool
def get_all_entities(domain_filter: str = "") -> str:
    """List all Home Assistant entities, optionally filtered by domain.
    Use this to discover available devices and their entity IDs.

    Args:
        domain_filter: Optional domain to filter by, e.g. 'light', 'climate', 'automation',
                       'switch', 'sensor'. Leave empty for all entities.
    """
    try:
        states = ha.get_all_states()
        if domain_filter:
            states = [s for s in states if s["entity_id"].startswith(f"{domain_filter}.")]
        lines = []
        for s in states:
            name = s.get("attributes", {}).get("friendly_name", "")
            lines.append(f"  {s['entity_id']} ({s['state']}){f' — {name}' if name else ''}")
        return f"Found {len(lines)} entities:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing entities: {e}"


@tool
def get_services(domain: str = "") -> str:
    """List available Home Assistant services, optionally filtered by domain.
    Use this to discover what actions can be performed on devices.

    Args:
        domain: Optional domain to filter by, e.g. 'light', 'climate', 'media_player'.
                Leave empty to list all domains and their service counts.
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
    """Render a Home Assistant Jinja2 template. Useful for complex queries like
    counting entities in a state, computing averages, or conditional logic.

    Args:
        template: A Jinja2 template string, e.g.
                  '{{ states.light | selectattr("state", "eq", "on") | list | count }}'
                  or '{{ state_attr("climate.bedroom", "current_temperature") }}'.
    """
    try:
        return ha.render_template(template)
    except Exception as e:
        return f"Error rendering template: {e}"


# --- Background Task Tools ---

@tool
def schedule_service(delay_seconds: int, domain: str, service: str, entity_id: str = "", data: str = "{}") -> str:
    """Schedule a Home Assistant service call to execute after a delay.
    Use this when the user says things like "in 5 minutes turn on the lights"
    or "turn off the TV in 30 seconds".

    Args:
        delay_seconds: How many seconds to wait before executing. Convert minutes to seconds (e.g. 3 minutes = 180).
        domain: The service domain, e.g. 'light', 'climate', 'automation'.
        service: The service to call, e.g. 'turn_on', 'turn_off', 'set_temperature'.
        entity_id: The target entity, e.g. 'light.living_room'.
        data: JSON string of additional service data. Defaults to '{}'.
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
    """Watch a Home Assistant entity and trigger a service call when it reaches a target state.
    This is a one-shot watcher — it fires once and cleans up. Not a persistent automation.

    Use this when the user says things like "turn on the lights when I get home",
    "start the heater when temperature drops below 60", or "lock the door when the sun sets".

    Args:
        watch_entity: The entity to monitor, e.g. 'person.neelabh', 'sensor.temperature', 'sun.sun'.
        target_state: The state value to wait for, e.g. 'home', 'below_horizon', 'on'.
                      For numeric sensors, use the number as a string (e.g. '60').
        then_domain: Service domain to call when condition is met, e.g. 'light', 'climate'.
        then_service: Service to call, e.g. 'turn_on', 'set_temperature'.
        then_entity: Target entity for the action, e.g. 'light.living_room'.
        then_data: JSON string of additional service data. Defaults to '{}'.
        poll_interval_seconds: How often to check state in seconds. Defaults to 5.
        timeout_minutes: Give up after this many minutes. Defaults to 60.
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
    """List all active background tasks (scheduled actions and watchers).
    Use this when the user asks what's running in the background, what's pending, etc.
    """
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
    """Cancel an active background task by its ID.
    Use list_active_tasks first to see available task IDs.

    Args:
        task_id: The ID of the task to cancel (e.g. 1, 2, 3).
    """
    with _task_lock:
        if task_id in _active_tasks:
            task = _active_tasks.pop(task_id)
            return f"Cancelled task #{task_id}: {task['description']}"
        return f"No active task with ID #{task_id}."


# --- Memory Tools ---

@tool
def save_memory(fact: str, is_core: bool = False) -> str:
    """Save a fact about the user or their home to persistent memory.
    Use this proactively when you learn something useful that will help in future conversations.

    Save as CORE (is_core=True) for facts that rarely change:
    - User's name, timezone, household members
    - Which person entity maps to which family member
    - Preferred temperature units (F vs C)
    - Pet names, routines, preferences

    Save as LEARNED (is_core=False) for everything else:
    - Device quirks ("bedroom light entity is light.bedroom_2")
    - Integrations ("user's car is Tesla, accessible via device_tracker.tesla")
    - Preferences that might change ("user likes lights at 80% brightness")

    Args:
        fact: The fact to remember. Be concise but specific.
        is_core: True for permanent facts (name, timezone, etc.), False for general learned facts.
    """
    return memory.add_memory(fact, is_core=is_core)


@tool
def forget_memory(fact: str) -> str:
    """Forget a previously saved memory. Use when the user says something is no longer true
    or asks you to forget something.

    Args:
        fact: The fact to forget. Must match an existing memory exactly.
    """
    return memory.forget_memory(fact)


all_tools = [
    call_service, get_state, get_all_entities, get_services, render_template,
    schedule_service, watch_and_act, list_active_tasks, cancel_task,
    save_memory, forget_memory,
]
