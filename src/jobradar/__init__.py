"""JobRadar: watch job channels, rank postings, build tailored resumes, track it all in Notion."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jobradar-agent")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"
