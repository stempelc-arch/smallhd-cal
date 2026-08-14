from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Patch:
    name: str
    r: float
    g: float
    b: float

    @property
    def rgb8(self) -> tuple[int, int, int]:
        return tuple(_float_to_u8(channel) for channel in (self.r, self.g, self.b))


@dataclass(frozen=True)
class Measurement:
    patch: Patch
    xyz: tuple[float, float, float]
    timestamp: str | None = None


def load_patch_sequence(path: str | Path) -> list[Patch]:
    patches: list[Patch] = []
    path = Path(path)

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [col for col in ("r", "g", "b") if col not in fieldnames]
        if missing:
            raise ValueError(
                f"{path}: missing required column(s) {', '.join(missing)} "
                f"(found: {', '.join(fieldnames) or 'none'})"
            )
        for index, row in enumerate(reader, start=1):
            name = row.get("patch_name") or row.get("name") or f"patch_{index:03d}"
            patches.append(
                Patch(
                    name=name,
                    r=_parse_channel(row["r"]),
                    g=_parse_channel(row["g"]),
                    b=_parse_channel(row["b"]),
                )
            )

    return patches


def write_measurements_json(path: str | Path, measurements: list[Measurement]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"measurements": [_measurement_to_json(item) for item in measurements]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_measurements_json(path: str | Path) -> list[Measurement]:
    path = Path(path)
    if not path.exists():
        return []

    payload = json.loads(path.read_text(encoding="utf-8"))
    return [_measurement_from_json(item) for item in payload["measurements"]]


def latest_measurements_by_patch(measurements: list[Measurement]) -> dict[str, Measurement]:
    return {measurement.patch.name: measurement for measurement in measurements}


def _parse_channel(raw: str) -> float:
    value = float(raw)
    if value > 1.0:
        value /= 255.0
    return max(0.0, min(1.0, value))


def _float_to_u8(value: float) -> int:
    return round(max(0.0, min(1.0, value)) * 255.0)


def _measurement_to_json(measurement: Measurement) -> dict[str, Any]:
    return {
        "patch": asdict(measurement.patch),
        "xyz": list(measurement.xyz),
        "timestamp": measurement.timestamp,
    }


def _measurement_from_json(item: dict[str, Any]) -> Measurement:
    patch = Patch(**item["patch"])
    return Measurement(
        patch=patch,
        xyz=tuple(float(value) for value in item["xyz"]),
        timestamp=item.get("timestamp"),
    )
