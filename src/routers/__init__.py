"""
routers package — the HTTP layer, split by resource.

WHY SPLIT INTO ROUTERS instead of one giant main.py:
  An APIRouter is a mini-app you can build in its own file and then "include"
  into the main app under a prefix. Benefits:
    - Organization: all task endpoints live in tasks.py, etc.
    - Prefix + tags in one place: every route in tasks.py gets "/... " and shows
      under the "tasks" group in /docs.
    - Scales: adding a feature = adding a router file, not editing a 2000-line
      module.

Routers stay THIN: validate inputs (via schemas), call a SERVICE, return a
response model. No business logic or SQL here — that's the service layer's job.
"""
