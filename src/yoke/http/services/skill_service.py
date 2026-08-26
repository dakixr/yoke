"""Skill discovery projections for HTTP clients."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.skills import load_skill_registry
from yoke.agent.skills.paths import default_skill_dirs
from yoke.http.errors import ApiError
from yoke.http.models.common import LocationInfo
from yoke.http.models.skill import SessionSkillData
from yoke.http.models.skill import SessionSkillResponse
from yoke.http.models.skill import SkillInfo
from yoke.http.models.skill import SkillListResponse
from yoke.session import SessionStore


class SkillService:
    """Read skill catalogs without starting provider runtimes."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def list_skills(
        self,
        *,
        directory: str | None,
        search: str | None,
    ) -> SkillListResponse:
        root = Path(directory or Path.cwd()).resolve()
        registry = load_skill_registry(default_skill_dirs(root))
        items = [_skill_info(spec, active=False) for spec in registry.skills]
        if search:
            needle = search.casefold()
            items = [
                item
                for item in items
                if needle in item.name.casefold()
                or needle in item.description.casefold()
            ]
        items.sort(key=lambda item: item.name)
        return SkillListResponse(
            location=LocationInfo(directory=str(root)),
            data=items,
        )

    def session_skills(self, session_id: str) -> SessionSkillResponse:
        record = self._require_record(session_id)
        root = Path(record.root or Path.cwd()).resolve()
        registry = load_skill_registry(default_skill_dirs(root))
        active_names = {skill.name for skill in record.active_skills}
        available = [
            _skill_info(spec, active=spec.name in active_names)
            for spec in registry.skills
        ]
        active = [
            SkillInfo(
                name=skill.name,
                description=skill.description,
                source_path=skill.source_path,
                active=True,
            )
            for skill in record.active_skills
        ]
        available.sort(key=lambda item: item.name)
        return SessionSkillResponse(
            data=SessionSkillData(active=active, available=available)
        )

    def _require_record(self, session_id: str):  # noqa: ANN202
        if not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")
        return self.store.load(session_id)


def _skill_info(spec, *, active: bool) -> SkillInfo:  # noqa: ANN001
    return SkillInfo(
        name=spec.name,
        description=spec.description,
        source_path=str(spec.skill_md_path),
        active=active,
    )
