"""hCaptcha solver package — checkbox and invisible modes."""
from .solve import (
    solve_hcaptcha,
    solve_hcaptcha_invisible,
    solve_hcaptcha_realpage,
)

__all__ = [
    "solve_hcaptcha",
    "solve_hcaptcha_invisible",
    "solve_hcaptcha_realpage",
]
