"""
schemas package — PYDANTIC MODELS that define the API's request/response shapes.

REMINDER (the single most important distinction in this codebase):
  - src/models/*  = SQLAlchemy ORM = the DATABASE shape (tables/columns).
  - src/schemas/* = Pydantic       = the HTTP API shape (what JSON goes in/out).

Why two separate layers instead of exposing ORM objects directly?
  1. SECURITY: the User table has hashed_password; the UserOut schema simply
     omits it, so it can never leak into a response.
  2. VALIDATION: Pydantic validates and coerces incoming JSON (types, lengths,
     ranges, emails) BEFORE your code runs. Bad input is rejected with a clear
     422 error automatically.
  3. DECOUPLING: you can restructure the DB without breaking your public API,
     and vice versa. The schema is a contract; the table is an implementation.
  4. DOCS: FastAPI turns these schemas into the OpenAPI spec at /docs for free.
"""
