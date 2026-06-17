from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from articleforgeai.core.paths import RUNS_DIR, safe_relative_to
from articleforgeai.models.db import Artifact
from sqlmodel import Session


class ArtifactStore:
    def __init__(self, root: Path = RUNS_DIR) -> None:
        self.root = root

    def run_dir(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(
        self,
        session: Session,
        run_id: str,
        filename: str,
        title: str,
        payload: Any,
        kind: str,
        summary: str = "",
    ) -> Artifact:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return self.write_text(
            session=session,
            run_id=run_id,
            filename=filename,
            title=title,
            text=text,
            kind=kind,
            media_type="application/json",
            summary=summary,
        )

    def write_markdown(
        self,
        session: Session,
        run_id: str,
        filename: str,
        title: str,
        text: str,
        kind: str,
        summary: str = "",
        persist_to_disk: bool = True,
    ) -> Artifact:
        if not persist_to_disk:
            return self.write_virtual_text(
                session=session,
                run_id=run_id,
                filename=filename,
                title=title,
                text=text,
                kind=kind,
                media_type="text/markdown; charset=utf-8",
                summary=summary,
            )
        return self.write_text(
            session=session,
            run_id=run_id,
            filename=filename,
            title=title,
            text=text,
            kind=kind,
            media_type="text/markdown; charset=utf-8",
            summary=summary,
        )

    def write_virtual_text(
        self,
        session: Session,
        run_id: str,
        filename: str,
        title: str,
        text: str,
        kind: str,
        media_type: str,
        summary: str = "",
    ) -> Artifact:
        content_for_preview = summary if summary else text
        artifact = Artifact(
            run_id=run_id,
            kind=kind,
            title=title,
            path=f"virtual://{run_id}/{filename}",
            media_type=media_type,
            summary=content_for_preview,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    def write_text(
        self,
        session: Session,
        run_id: str,
        filename: str,
        title: str,
        text: str,
        kind: str,
        media_type: str,
        summary: str = "",
    ) -> Artifact:
        path = self.run_dir(run_id) / filename
        safe_relative_to(path, self.root)
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        artifact = Artifact(
            run_id=run_id,
            kind=kind,
            title=title,
            path=str(path.relative_to(self.root)),
            media_type=media_type,
            summary=summary,
            sha256=digest,
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    def resolve(self, artifact: Artifact) -> Path:
        return safe_relative_to(self.root / artifact.path, self.root)


artifact_store = ArtifactStore()
