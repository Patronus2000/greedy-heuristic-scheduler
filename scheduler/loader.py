"""
Scenario Loader

Reads a scenario JSON file and returns a validated Scenario model.
All business logic stays in the engine — the loader is purely structural.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import Scenario


def load_scenario(path: str | Path) -> Scenario:
    """
    Load and validate a scenario from a JSON file.
    Raises pydantic.ValidationError if the JSON is structurally invalid.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return Scenario.model_validate(data)


def load_all_scenarios(scenarios_dir: str | Path) -> list[Scenario]:
    """
    Load all scenario JSON files from a directory, sorted by filename.
    Returns a list of validated Scenario objects.
    """
    scenarios_dir = Path(scenarios_dir)
    paths = sorted(scenarios_dir.glob("*.json"))
    return [load_scenario(p) for p in paths]
