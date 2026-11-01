"""Vercel entry point for the password-protected hosted demo."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ax.api import app  # noqa: E402,F401
