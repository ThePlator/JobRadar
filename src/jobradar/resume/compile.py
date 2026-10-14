"""Compile .tex to PDF with Tectonic (preferred) or pdflatex, in a scratch folder.

Tectonic runs with `--untrusted` (no shell escape, no external tools); pdflatex with
`-no-shell-escape`. Either has 60 seconds. On failure the LaTeX log is kept next to where
the PDF would have gone, so you can see what broke.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

TIMEOUT = 60


class CompileError(Exception):
    """The .tex didn't compile; the message says why (and where the log is)."""


def engine() -> list[str] | None:
    """The command line to compile `resume.tex`, or None when no engine is installed."""
    if tectonic := shutil.which("tectonic"):
        return [tectonic, "-X", "compile", "--untrusted", "--keep-logs", "resume.tex"]
    if pdflatex := shutil.which("pdflatex"):
        return [pdflatex, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
                "resume.tex"]  # fmt: skip
    return None


INSTALL_HINT = (
    "No LaTeX engine found. Install Tectonic (https://tectonic-typesetting.github.io, one "
    "binary; it fetches the LaTeX packages it needs) or TeX Live: "
    "`sudo apt install texlive-latex-extra`."
)


async def compile_pdf(tex: str, pdf_path: Path) -> Path:
    """Compile and write the PDF to `pdf_path`. Raises CompileError."""
    command = engine()
    if command is None:
        raise CompileError(INSTALL_HINT)
    with tempfile.TemporaryDirectory(prefix="jobradar-tex-") as tmp:
        work = Path(tmp)
        (work / "resume.tex").write_text(tex, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=work,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise CompileError(f"LaTeX took longer than {TIMEOUT} s") from None
        built = work / "resume.pdf"
        if proc.returncode != 0 or not built.is_file():
            log_path = pdf_path.with_suffix(".log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = work / "resume.log"
            text = log_file.read_text(errors="replace") if log_file.is_file() else ""
            log_path.write_text(text + "\n" + output.decode(errors="replace"))
            raise CompileError(f"LaTeX failed; see {log_path}")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(built, pdf_path)
    return pdf_path
