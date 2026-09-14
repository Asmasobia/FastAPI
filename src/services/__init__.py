"""
services package — THE BUSINESS-LOGIC LAYER.

WHY A SERVICE LAYER (and not just DB calls inside routers)?
  Routers should be THIN: parse the request, call a service, shape the response.
  Everything else — "does this project exist?", "does this user own it?", the
  actual SQL, the cascade rules — lives in services. Benefits:
    - Reusable: a background job or CLI can call the same service functions.
    - Testable: business logic is testable without spinning up HTTP.
    - Readable: routers read like a table of contents; services hold the detail.
    - Framework-independent: services speak in domain terms and raise domain
      errors (see exceptions.py), never HTTP.

Each service module owns one entity's operations (users, projects, tasks,
comments) and receives an AsyncSession to work with — it never creates its own,
so it always participates in the request's transaction/session lifecycle.
"""
