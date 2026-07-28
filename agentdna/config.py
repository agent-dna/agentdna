import json
import os

from dataclasses import asdict
from pathlib import Path

from .types import (
    ActorRegistryEntry,
    ACTOR_INFO_FILE,
)


# --------- Actor Config functions ----------------------
def get_default_config_dir() -> str:
    return os.path.join(Path.home(), ".agentdna")


def load_actor_config(
    config_dir: str,
) -> list[ActorRegistryEntry]:
    """
    Loads actor registry entries from actor_info.json.
    """

    actor_config_path = os.path.join(
        config_dir,
        ACTOR_INFO_FILE,
    )

    if not os.path.exists(actor_config_path):
        return []

    with open(actor_config_path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    return [ActorRegistryEntry(**entry) for entry in data]


def save_actor_config(
    config_dir: str,
    entries: list[ActorRegistryEntry],
) -> None:
    """
    Saves actor registry entries to actor_info.json.
    """

    os.makedirs(config_dir, exist_ok=True)

    actor_config_path = os.path.join(
        config_dir,
        ACTOR_INFO_FILE,
    )

    with open(actor_config_path, "w", encoding="utf-8") as fp:
        json.dump(
            [asdict(entry) for entry in entries],
            fp,
            indent=4,
        )


def get_actor_registry_entry(
    config_dir: str,
    actor_id: str,
) -> ActorRegistryEntry | None:
    """
    Finds an actor registry entry by actor ID.
    """

    entries = load_actor_config(config_dir)

    for entry in entries:
        if entry.actor_id == actor_id:
            return entry

    return None


def upsert_actor_registry_entry(
    config_dir: str,
    entry: ActorRegistryEntry,
) -> None:
    """
    Inserts or updates an actor registry entry.
    """

    entries = load_actor_config(config_dir)

    for idx, existing in enumerate(entries):
        if existing.actor_id == entry.actor_id:
            entries[idx] = entry

            save_actor_config(
                config_dir,
                entries,
            )
            return

    entries.append(entry)

    save_actor_config(
        config_dir,
        entries,
    )


# ------------------------------------------------------
