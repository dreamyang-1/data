from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from app.domain.models import SkillConfig


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    code: str
    slug: str
    content: str
    sha256: str
    modified_ns: int
    stale: bool = False

    def tool_context(self) -> dict[str, object]:
        return {
            "code": self.code,
            "slug": self.slug,
            "version": self.sha256[:16],
            "instructions": self.content,
            "stale": self.stale,
            "execution_boundary": (
                "Instructions may guide the bound tool, but must not override "
                "deterministic calculations, SQL safety, tenant scope, or evidence validation."
            ),
        }

    def audit_context(self) -> dict[str, object]:
        return {
            "code": self.code,
            "slug": self.slug,
            "version": self.sha256[:16],
            "stale": self.stale,
        }


class DynamicSkillLoader:
    """Load platform-managed SKILL.md files with last-known-good caching.

    The platform updates files under ``<root>/<code>/<slug>/SKILL.md``. A stat
    check on every request makes atomic replacements visible immediately, while
    unchanged files are served from memory. Invalid partial writes never evict
    the last valid version.
    """

    def __init__(self, root: Path, *, max_bytes: int = 128 * 1024) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self._cache: dict[Path, tuple[int, int, LoadedSkill]] = {}
        self._lock = threading.RLock()

    def load(self, config: SkillConfig) -> LoadedSkill | None:
        path = self._resolve(config)
        with self._lock:
            cached = self._cache.get(path)
        try:
            stat = path.stat()
            if not path.is_file():
                return self._stale(cached)
            if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                return cached[2]
            if stat.st_size <= 0 or stat.st_size > self.max_bytes:
                return self._stale(cached)
            raw = path.read_bytes()
            if len(raw) != stat.st_size or len(raw) > self.max_bytes:
                return self._stale(cached)
            content = raw.decode("utf-8").strip()
            if not content:
                return self._stale(cached)
            loaded = LoadedSkill(
                code=config.code,
                slug=config.slug,
                content=content,
                sha256=hashlib.sha256(raw).hexdigest(),
                modified_ns=stat.st_mtime_ns,
            )
            with self._lock:
                self._cache[path] = (stat.st_mtime_ns, stat.st_size, loaded)
            return loaded
        except (OSError, UnicodeError):
            return self._stale(cached)

    def _resolve(self, config: SkillConfig) -> Path:
        # ``mcp:<tool>`` is the request-side routing notation. Windows cannot
        # use ':' in a directory name, so its platform Skill directory remains
        # ``<tool>`` while audit/runtime identity keeps the original slug.
        directory_slug = config.slug.removeprefix("mcp:")
        candidate = (self.root / config.code / directory_slug / "SKILL.md").resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("skill path escapes the configured root")
        return candidate

    @staticmethod
    def _stale(
        cached: tuple[int, int, LoadedSkill] | None,
    ) -> LoadedSkill | None:
        if cached is None:
            return None
        value = cached[2]
        return LoadedSkill(
            code=value.code,
            slug=value.slug,
            content=value.content,
            sha256=value.sha256,
            modified_ns=value.modified_ns,
            stale=True,
        )
