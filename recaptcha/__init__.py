"""reCAPTCHA solver package — v3, invisible, and v2 checkbox."""
from .solve import (
    solve_recaptcha_v2,
    solve_recaptcha_v2_realpage,
    solve_recaptcha_v3,
    solve_recaptcha_v3_realpage,
    solve_recaptcha_invisible,
    solve_recaptcha_invisible_realpage,
)

__all__ = [
    "solve_recaptcha_v3",
    "solve_recaptcha_v3_realpage",
    "solve_recaptcha_invisible",
    "solve_recaptcha_invisible_realpage",
    "solve_recaptcha_v2",
    "solve_recaptcha_v2_realpage",
]
