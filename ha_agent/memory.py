import json
import os
import threading
from datetime import datetime

MEMORY_FILE = os.getenv("MEMORY_FILE", "data/memories.json")
CORE_CAP = 10
LEARNED_CAP = 20

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {"core": [], "learned": []}
    with open(MEMORY_FILE) as f:
        return json.load(f)


def _save(data: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE) or ".", exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_memory(fact: str, is_core: bool = False) -> str:
    with _lock:
        data = _load()
        entry = {"fact": fact, "saved_at": datetime.now().isoformat()}
        category = "core" if is_core else "learned"

        # Dedupe — don't save if a very similar fact already exists
        for existing in data[category]:
            if existing["fact"].lower() == fact.lower():
                return f"Already remembered: {fact}"

        cap = CORE_CAP if is_core else LEARNED_CAP
        if len(data[category]) >= cap:
            dropped = data[category].pop(0)
            data[category].append(entry)
            _save(data)
            return f"Remembered (dropped oldest: \"{dropped['fact']}\"). {category.title()} memory at cap ({cap})."

        data[category].append(entry)
        _save(data)
        return f"Remembered as {category}: {fact}"


def forget_memory(fact: str) -> str:
    with _lock:
        data = _load()
        for category in ["core", "learned"]:
            for i, entry in enumerate(data[category]):
                if entry["fact"].lower() == fact.lower():
                    data[category].pop(i)
                    _save(data)
                    return f"Forgot: {fact}"
        return f"No matching memory found for: {fact}"


def get_all_memories() -> dict:
    with _lock:
        return _load()


def format_memories_for_prompt() -> str:
    data = get_all_memories()
    if not data["core"] and not data["learned"]:
        return ""

    lines = ["\n--- Your memories about this user and home ---"]

    if data["core"]:
        lines.append("Core (permanent):")
        for m in data["core"]:
            lines.append(f"  - {m['fact']}")

    if data["learned"]:
        lines.append(f"Learned ({len(data['learned'])}/{LEARNED_CAP}):")
        for m in data["learned"]:
            lines.append(f"  - {m['fact']}")

    return "\n".join(lines)
