"""Access control rules: plain code, unit tested, never delegated to a prompt."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.domain.errors import UnknownUserError


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass(frozen=True)
class AccessPolicy:
    field_to_level: dict[str, str]          # normalised Access text -> level
    role_levels: dict[str, frozenset[str]]

    @classmethod
    def load(cls, path: str | Path) -> "AccessPolicy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            field_to_level={_norm(k): v for k, v in raw["access_field_to_level"].items()},
            role_levels={r: frozenset(lv) for r, lv in raw["role_levels"].items()},
        )

    def level_for_access_field(self, access_field: str) -> str | None:
        """Exact (whitespace/case-normalised) match only. Unknown text -> None, and
        the caller must fail closed (the document is not indexed)."""
        return self.field_to_level.get(_norm(access_field))

    def levels_for_role(self, role: str) -> frozenset[str]:
        # Unknown role -> no levels at all (fail closed).
        return self.role_levels.get(role, frozenset())

    def can_read(self, role: str, access_level: str) -> bool:
        return access_level in self.levels_for_role(role)


@dataclass(frozen=True)
class StaffUser:
    user_id: str
    name: str
    role: str


class UserDirectory:
    """Resolves user_id -> role. In production this would be the SSO/IdP claim."""

    def __init__(self, users: dict[str, StaffUser]):
        self._users = users

    @classmethod
    def load_csv(cls, path: str | Path) -> "UserDirectory":
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return cls({r["user_id"].strip(): StaffUser(r["user_id"].strip(), r["name"].strip(), r["role"].strip()) for r in rows})

    def get(self, user_id: str) -> StaffUser:
        u = self._users.get((user_id or "").strip())
        if u is None:
            raise UnknownUserError("Unknown user id.", {"user_id": user_id})
        return u
