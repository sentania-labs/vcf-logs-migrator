"""Client settings in a plain INI file. Never credentials; those stay in session memory."""

from __future__ import annotations

import configparser
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_DIR = "vcf-logs-migrator"
THEMES = ("light", "dark")


def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / APP_DIR


def default_export_folder() -> str:
    return str(Path.home() / "Documents" / "VCF Logs Migrator" / "Reports")


def default_journal_path() -> str:
    return str(config_dir() / "journal.sqlite")


def local_timezone() -> str:
    """The OS zone name when it is a valid IANA key; otherwise UTC."""
    from zoneinfo import ZoneInfo

    for candidate in (os.environ.get("TZ"), _etc_localtime()):
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except Exception:
                continue
    return "UTC"


def _etc_localtime() -> str | None:
    try:
        target = Path("/etc/localtime").resolve()
    except OSError:
        return None
    parts = target.parts
    if "zoneinfo" in parts:
        return "/".join(parts[parts.index("zoneinfo") + 1 :])
    return None


@dataclass
class Settings:
    """Everything the operator can change from the Settings page."""

    theme: str = "light"
    timezone: str = "UTC"
    refresh_seconds: int = 5
    verify_budget_minutes: int = 5
    count_poll_seconds: int = 30
    export_folder: str = ""
    journal_path: str = ""
    fixture_path: str = ""
    source_host: str = ""
    source_ssh_user: str = "root"
    source_api_user: str = "admin"
    destination_host: str = ""
    destination_api_user: str = "admin"
    ops_url: str = ""

    def __post_init__(self):
        if self.theme not in THEMES:
            self.theme = "light"
        self.refresh_seconds = _clamp(self.refresh_seconds, 1, 3600)
        self.verify_budget_minutes = _clamp(self.verify_budget_minutes, 1, 1440)
        self.count_poll_seconds = _clamp(self.count_poll_seconds, 0, 3600)
        if not self.export_folder:
            self.export_folder = default_export_folder()
        if not self.journal_path:
            self.journal_path = default_journal_path()
        if not self.timezone:
            self.timezone = "UTC"

    @property
    def verify_attempts(self) -> int:
        """Engine Policy attempts derived from the retry budget and poll interval."""
        if self.count_poll_seconds == 0:
            return 2
        return max(2, min(10000, self.verify_budget_minutes * 60 // self.count_poll_seconds))

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or config_dir() / "settings.ini"
        parser = configparser.ConfigParser(interpolation=None)
        values: dict[str, object] = {}
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError):
            return cls(timezone=local_timezone())
        section = parser["client"] if parser.has_section("client") else {}
        for field in fields(cls):
            if field.name not in section:
                continue
            raw = section[field.name]
            if field.type == "int":
                try:
                    values[field.name] = int(raw)
                except ValueError:
                    continue
            else:
                values[field.name] = raw
        values.setdefault("timezone", local_timezone())
        return cls(**values)

    def save(self, path: Path | None = None) -> Path:
        path = path or config_dir() / "settings.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser(interpolation=None)
        parser["client"] = {key: str(value) for key, value in asdict(self).items()}
        with open(path, "w", encoding="utf-8") as handle:
            parser.write(handle)
        return path


def _clamp(value: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low
