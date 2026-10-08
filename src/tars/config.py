"""Configuration loaded from a TOML file.

Secrets (API keys, tokens) never live here; see ``tars.secrets``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


def default_home() -> Path:
    """Where TARS keeps its config and data: %APPDATA%\\TARS on Windows, ~/.tars elsewhere."""
    if env := os.environ.get("TARS_HOME"):
        return Path(env)
    if appdata := os.environ.get("APPDATA"):
        return Path(appdata) / "TARS"
    return Path.home() / ".tars"


@dataclass
class BrainConfig:
    # Haiku 5.5 handles everyday requests; Sonnet 5.5 takes over multi-step tasks.
    fast_model: str = "claude-haiku-5-5"
    fast_effort: str = "low"
    fast_thinking: bool = False  # Haiku 5.5 accepts thinking off at effort low/medium/high
    deep_model: str = "claude-sonnet-5-5"
    deep_effort: str = "medium"
    max_tokens: int = 4096
    max_tool_rounds: int = 8


@dataclass
class VoiceConfig:
    push_to_talk_key: str = "<ctrl>+<alt>+space"
    mute_key: str = "<ctrl>+<alt>+m"
    follow_up_seconds: float = 8.0
    stt_model: str = "flux-general-en"
    tts_model: str = "eleven_flash_v2_5"
    tts_voice_id: str = ""  # an original ElevenLabs voice you pick; never a clone of an actor
    input_device_index: int | None = None
    output_device_index: int | None = None
    sample_rate: int = 16000


@dataclass
class SpendConfig:
    monthly_cap_usd: float = 25.0
    # Above this share of the cap, TARS stops escalating to the deep model and says so.
    escalation_cutoff: float = 0.8
    # Usage-billed speech prices; check your plan and adjust.
    deepgram_usd_per_minute: float = 0.0077
    elevenlabs_usd_per_1k_chars: float = 0.05


@dataclass
class AlertConfig:
    max_per_day: int = 3
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"


@dataclass
class TodoistConfig:
    shopping_project: str = "Shopping"
    default_store: str = "Grocery"


@dataclass
class LocationConfig:
    home_address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = ""  # IANA name; empty means the system timezone


@dataclass
class PrivacyConfig:
    transcript_days: int = 7
    backup_days: int = 30


@dataclass
class Config:
    home: Path = field(default_factory=default_home)
    vault_path: Path | None = None
    backup_path: Path | None = None
    user_name: str = ""
    brain: BrainConfig = field(default_factory=BrainConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    spend: SpendConfig = field(default_factory=SpendConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    todoist: TodoistConfig = field(default_factory=TodoistConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def vault(self) -> Path:
        return self.vault_path or self.home / "vault"

    @property
    def backups(self) -> Path:
        return self.backup_path or self.home / "vault-backups"


def _merge(obj: Any, values: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Unknown config key: {key}")
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        elif key in ("home", "vault_path", "backup_path"):
            setattr(obj, key, Path(value).expanduser())
        else:
            setattr(obj, key, value)
    return obj


def load_config(path: Path | None = None) -> Config:
    config = Config()
    path = path or config.home / "config.toml"
    if path.exists():
        with path.open("rb") as f:
            _merge(config, tomllib.load(f))
    return config
