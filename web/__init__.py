"""Admin web dashboard for the captcha-solver sidecar.

  auth.py    single-admin credential, server-side sessions, CSRF, throttling
  routes.py  /api/v1 JSON API + the SPA shell (built from ui/ into ui/dist)

The dashboard is optional: without SOLVER_ADMIN_USER and a password it stays
404, and the solver behaves exactly as before.
"""
