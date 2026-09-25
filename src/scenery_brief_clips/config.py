from __future__ import annotations

import json
import math
from pathlib import Path


class ConfigError(ValueError):
    pass


_INTEGER_KEYS = {
    "max_search_results",
    "max_metadata_fetches",
    "min_height",
    "min_width",
    "max_tiles",
    "max_rank_videos",
    "max_analyze_videos",
    "continuity_max_adjacent_hamming",
    "continuity_max_endpoint_hamming",
}
_FLOAT_KEYS = {
    "sleep_s",
    "aspect_min",
    "aspect_max",
    "max_analysis_s",
    "continuity_sample_fps",
    "continuity_max_adjacent_color",
    "continuity_max_endpoint_color",
}
_JEV_FLOAT_KEYS = {"jev_reject_below", "jev_keep_above", "jev_timeout_s", "jev_max_usd_per_run"}
_JEV_BOOL_KEYS = {"jev_gate", "jev_order_by_p"}
_ALLOWED_KEYS = {
    *_JEV_FLOAT_KEYS,
    *_JEV_BOOL_KEYS,
    "allow_download",
    "allow_export",
    "export_max_height",
    "continuity_enabled",
    *_INTEGER_KEYS,
    *_FLOAT_KEYS,
}


def _parse_scalar(text: str):
    value = text.strip()
    if not value:
        raise ConfigError("config value cannot be empty")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _validate_config(config: dict) -> dict:
    unknown = sorted(set(config) - _ALLOWED_KEYS)
    if unknown:
        raise ConfigError(f"unknown config key: {unknown[0]}")
    if config.get("allow_download", False) is not False:
        raise ConfigError("allow_download must remain false (full-source downloads are never allowed)")
    if not isinstance(config.get("allow_export", False), bool):
        raise ConfigError("allow_export must be a boolean")
    if "continuity_enabled" in config and not isinstance(config.get("continuity_enabled"), bool):
        raise ConfigError("continuity_enabled must be a boolean")
    if "export_max_height" in config:
        value = config["export_max_height"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not (720 <= value <= 2160)
        ):
            raise ConfigError("export_max_height must be an integer between 720 and 2160")

    for key in _INTEGER_KEYS:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} must be an integer")
        if key in {"continuity_max_adjacent_hamming", "continuity_max_endpoint_hamming"}:
            if not (0 <= value <= 64):
                raise ConfigError(f"{key} must be an integer between 0 and 64")
            continue
        minimum = 0 if key == "max_metadata_fetches" else 1
        if value < minimum:
            raise ConfigError(f"{key} must be at least {minimum}")

    for key in _FLOAT_KEYS:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number")
        value = float(value)
        minimum = 0.0 if key == "sleep_s" else 0.0
        if not math.isfinite(value) or value < minimum or (key != "sleep_s" and value == 0):
            relation = "at least 0" if key == "sleep_s" else "greater than 0"
            raise ConfigError(f"{key} must be finite and {relation}")
        config[key] = value

    for key in _JEV_BOOL_KEYS:
        if key in config and not isinstance(config[key], bool):
            raise ConfigError(f"{key} must be a boolean")
    for key in _JEV_FLOAT_KEYS:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ConfigError(f"{key} must be a finite number")
        value = float(value)
        if key in {"jev_reject_below", "jev_keep_above"} and not (0.0 <= value <= 1.0):
            raise ConfigError(f"{key} must be between 0 and 1")
        if key in {"jev_timeout_s", "jev_max_usd_per_run"} and value <= 0:
            raise ConfigError(f"{key} must be greater than 0")
        config[key] = value
    if config.get("jev_reject_below", 0.40) >= config.get("jev_keep_above", 0.85):
        raise ConfigError("jev_reject_below must be below jev_keep_above")

    aspect_min = config.get("aspect_min")
    aspect_max = config.get("aspect_max")
    if aspect_min is not None and aspect_max is not None and aspect_min > aspect_max:
        raise ConfigError("aspect_min cannot exceed aspect_max")
    return config


def load_project_config(
    project_root: str | Path,
    config_path: str | Path | None = None,
) -> dict:
    root = Path(project_root).resolve()
    explicit = config_path is not None
    path = Path(config_path) if explicit else root / "config.yaml"
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ConfigError("config file must stay inside the project root")
    if not path.is_file():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return {}

    parsed: dict = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ConfigError(f"invalid flat YAML at line {line_number}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"missing config key at line {line_number}")
        if key in parsed:
            raise ConfigError(f"duplicate config key: {key}")
        parsed[key] = _parse_scalar(value)
    return _validate_config(parsed)
