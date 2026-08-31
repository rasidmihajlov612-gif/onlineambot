from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "training.yaml"

with open(CONFIG_PATH, encoding="utf-8") as f:
    _config = yaml.safe_load(f)

STEPS = _config["steps"]
ADMISSION = _config["admission"]

_step_by_id = {s["id"]: s for s in STEPS}
_step_ids = [s["id"] for s in STEPS]


def get_step(step_id):
    return _step_by_id[step_id]


def first_step_id():
    return _step_ids[0]


def next_step_id(step_id):
    idx = _step_ids.index(step_id)
    return _step_ids[idx + 1] if idx + 1 < len(_step_ids) else None
