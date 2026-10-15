"""Local PDF storage: `{company}_{role}_{yyyymmdd}_v{n}.pdf` in `storage.local_dir`."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path


def _part(text: str | None, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    return "".join(w[:1].upper() + w[1:] for w in words)[:40] or fallback


def resume_path(
    directory: Path, company: str | None, role: str | None, day: date, version: int
) -> Path:
    """data/resumes/Stripe_SoftwareEngineerIntern_20261014_v1.pdf"""
    name = f"{_part(company, 'Company')}_{_part(role, 'Role')}_{day:%Y%m%d}_v{version}.pdf"
    return directory / name
