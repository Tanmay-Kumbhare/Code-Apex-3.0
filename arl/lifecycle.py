"""
ARL Ruleset Lifecycle and Publication Framework.

Implements Part 12 and Part 13 publication requirements:
  - Immutable published ruleset versions.
  - Semantic versioning in MAJOR.MINOR.PATCH form.
  - Editing a published ruleset automatically creates a new draft version.
  - Strict immutability enforcement: cannot alter a PUBLISHED version.
  - Pre-publication compiler gate verification.
  - Audit logging and version comparison.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import Ruleset
from arl.errors import ArlError, ErrorCollector, Phase, Severity
from arl.parser import parse_ruleset
from arl.resolve import Resolver
from arl.revision import RulesetDiff, diff_rulesets
from arl.schema import Schema, get_base_schema
from arl.typecheck import TypeChecker


# =========================================================================
# Semantic Versioning
# =========================================================================

class SemVer(BaseModel):
    """Semantic versioning in MAJOR.MINOR.PATCH form."""
    major: int = 1
    minor: int = 0
    patch: int = 0

    @classmethod
    def parse(cls, version_str: str) -> SemVer:
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version_str.strip())
        if not match:
            raise ValueError(f"Invalid semantic version '{version_str}'. Must follow MAJOR.MINOR.PATCH format.")
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
        )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump_patch(self) -> SemVer:
        return SemVer(major=self.major, minor=self.minor, patch=self.patch + 1)

    def bump_minor(self) -> SemVer:
        return SemVer(major=self.major, minor=self.minor + 1, patch=0)

    def bump_major(self) -> SemVer:
        return SemVer(major=self.major + 1, minor=0, patch=0)

    def bump(self, kind: str = "patch") -> SemVer:
        kind_lower = kind.lower().strip()
        if kind_lower == "major":
            return self.bump_major()
        elif kind_lower == "minor":
            return self.bump_minor()
        elif kind_lower == "patch":
            return self.bump_patch()
        else:
            raise ValueError(f"Unknown version bump type '{kind}'. Choose 'major', 'minor', or 'patch'.")

    def __lt__(self, other: SemVer) -> bool:
        return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

    def __le__(self, other: SemVer) -> bool:
        return (self.major, self.minor, self.patch) <= (other.major, other.minor, other.patch)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SemVer):
            return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch)
        if isinstance(other, str):
            try:
                parsed = SemVer.parse(other)
                return self == parsed
            except ValueError:
                return False
        return False


# =========================================================================
# Ruleset Status and Version Record
# =========================================================================

class RulesetStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class RulesetVersionRecord(BaseModel):
    """Immutable audit record of a specific ruleset version."""
    ruleset_name: str
    version: str
    status: RulesetStatus = RulesetStatus.DRAFT
    source_code: str
    author: str = "system"
    commit_message: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    published_at: Optional[str] = None
    content_hash: str = ""
    is_immutable: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if not self.content_hash and self.source_code:
            self.content_hash = hashlib.sha256(self.source_code.encode("utf-8")).hexdigest()

    def parse_ast(self, collector: Optional[ErrorCollector] = None) -> Ruleset:
        """Parse source code into AST."""
        return parse_ruleset(self.source_code, collector=collector)


# =========================================================================
# Lifecycle Manager / Registry
# =========================================================================

class LifecycleManager:
    """
    Manages publication, immutability, semantic versioning, and version history.
    """

    def __init__(
        self,
        schema: Optional[Schema] = None,
        storage_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.schema = schema or get_base_schema()
        self.storage_dir = Path(storage_dir) if storage_dir else None
        # Registry: ruleset_name -> version_str -> RulesetVersionRecord
        self.registry: Dict[str, Dict[str, RulesetVersionRecord]] = {}

        if self.storage_dir and self.storage_dir.exists():
            self._load_from_storage()

    def create_ruleset(
        self,
        name: str,
        source: str,
        initial_version: str = "1.0.0",
        author: str = "system",
        commit_message: str = "Initial draft",
    ) -> RulesetVersionRecord:
        """Create a new ruleset draft."""
        ver = SemVer.parse(initial_version)
        v_str = str(ver)

        if name in self.registry and v_str in self.registry[name]:
            raise ValueError(f"Ruleset '{name}' version {v_str} already exists.")

        record = RulesetVersionRecord(
            ruleset_name=name,
            version=v_str,
            status=RulesetStatus.DRAFT,
            source_code=source,
            author=author,
            commit_message=commit_message,
            is_immutable=False,
        )

        self.registry.setdefault(name, {})[v_str] = record
        self._save_to_storage(record)
        return record

    def publish(
        self,
        name: str,
        version: str,
        commit_message: str = "Published release",
        author: str = "system",
    ) -> RulesetVersionRecord:
        """
        Publish a ruleset version, making it permanently immutable.
        Verifies full compiler gate before allowing publication.
        """
        record = self.get_version(name, version)
        if record.status == RulesetStatus.PUBLISHED:
            raise ValueError(f"Ruleset '{name}' version {version} is already published.")

        # Compiler gate: ensure ruleset parses, typechecks, and resolves cleanly
        col = ErrorCollector()
        rs = parse_ruleset(record.source_code, collector=col)
        if col.has_fatal:
            raise ValueError(f"Cannot publish ruleset '{name}' {version}: parsing failed with {len(col.errors)} errors.")

        tc = TypeChecker(self.schema, collector=col)
        tc.check_ruleset(rs)
        resolver = Resolver(self.schema, collector=col)
        resolver.resolve(rs)

        if col.has_fatal:
            err_msgs = [e.message for e in col.errors if e.severity == Severity.FATAL]
            raise ValueError(f"Cannot publish ruleset '{name}' {version}: typechecking/resolution failed: {'; '.join(err_msgs)}")

        # Mark as published and immutable
        record.status = RulesetStatus.PUBLISHED
        record.is_immutable = True
        record.published_at = datetime.now(timezone.utc).isoformat()
        if commit_message:
            record.commit_message = commit_message
        if author:
            record.author = author

        self._save_to_storage(record)
        return record

    def edit_ruleset(
        self,
        name: str,
        new_source: str,
        bump: str = "patch",
        author: str = "system",
        commit_message: str = "Update ruleset",
    ) -> RulesetVersionRecord:
        """
        Edit a ruleset. If current version is published, enforce immutability
        by automatically creating a new version bumped according to `bump`.
        If current is a draft, update the draft in place.
        """
        latest = self.get_latest_version(name)

        if latest.is_immutable or latest.status == RulesetStatus.PUBLISHED:
            # Immutability rule: MUST create a new version!
            current_ver = SemVer.parse(latest.version)
            next_ver = current_ver.bump(bump)
            next_ver_str = str(next_ver)

            # Ensure version does not collide
            while next_ver_str in self.registry[name]:
                next_ver = next_ver.bump("patch")
                next_ver_str = str(next_ver)

            record = RulesetVersionRecord(
                ruleset_name=name,
                version=next_ver_str,
                status=RulesetStatus.DRAFT,
                source_code=new_source,
                author=author,
                commit_message=f"Created from {latest.version}: {commit_message}",
                is_immutable=False,
            )
            self.registry[name][next_ver_str] = record
            self._save_to_storage(record)
            return record
        else:
            # Current version is draft: update in place
            record = RulesetVersionRecord(
                ruleset_name=name,
                version=latest.version,
                status=RulesetStatus.DRAFT,
                source_code=new_source,
                author=author,
                commit_message=commit_message,
                is_immutable=False,
            )
            self.registry[name][latest.version] = record
            self._save_to_storage(record)
            return record

    def deprecate(self, name: str, version: str) -> RulesetVersionRecord:
        """Mark a published version as deprecated."""
        record = self.get_version(name, version)
        record.status = RulesetStatus.DEPRECATED
        self._save_to_storage(record)
        return record

    def get_version(self, name: str, version: Optional[str] = None) -> RulesetVersionRecord:
        """Retrieve a specific ruleset version or the latest."""
        if name not in self.registry or not self.registry[name]:
            raise KeyError(f"Ruleset '{name}' not found in registry.")

        if version is None:
            return self.get_latest_version(name)

        if version not in self.registry[name]:
            raise KeyError(f"Version '{version}' of ruleset '{name}' not found.")

        return self.registry[name][version]

    def get_latest_version(self, name: str) -> RulesetVersionRecord:
        """Get highest semantic version for ruleset."""
        if name not in self.registry or not self.registry[name]:
            raise KeyError(f"Ruleset '{name}' not found in registry.")

        versions = sorted(
            [SemVer.parse(v) for v in self.registry[name].keys()]
        )
        latest_ver_str = str(versions[-1])
        return self.registry[name][latest_ver_str]

    def get_latest_published(self, name: str) -> Optional[RulesetVersionRecord]:
        """Get highest semantic version that is PUBLISHED."""
        if name not in self.registry:
            return None

        pub_versions = [
            SemVer.parse(v)
            for v, rec in self.registry[name].items()
            if rec.status == RulesetStatus.PUBLISHED
        ]
        if not pub_versions:
            return None
        pub_versions.sort()
        return self.registry[name][str(pub_versions[-1])]

    def list_rulesets(self) -> List[str]:
        return sorted(list(self.registry.keys()))

    def list_versions(self, name: str) -> List[RulesetVersionRecord]:
        """List all version records for a ruleset ordered by SemVer."""
        if name not in self.registry:
            return []

        sorted_keys = sorted([SemVer.parse(v) for v in self.registry[name].keys()])
        return [self.registry[name][str(k)] for k in sorted_keys]

    def diff_versions(self, name: str, v1: str, v2: str) -> RulesetDiff:
        """Compute structured rule differences between two versions of a ruleset."""
        rec1 = self.get_version(name, v1)
        rec2 = self.get_version(name, v2)

        rs1 = parse_ruleset(rec1.source_code)
        rs2 = parse_ruleset(rec2.source_code)

        return diff_rulesets(rs1, rs2)

    def _save_to_storage(self, record: RulesetVersionRecord) -> None:
        if not self.storage_dir:
            return
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{record.ruleset_name}_{record.version}.json"
        path = self.storage_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            f.write(record.model_dump_json(indent=2))

    def _load_from_storage(self) -> None:
        if not self.storage_dir or not self.storage_dir.exists():
            return
        for file in self.storage_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                rec = RulesetVersionRecord(**data)
                self.registry.setdefault(rec.ruleset_name, {})[rec.version] = rec
            except Exception:
                pass
