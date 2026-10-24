"""The service layer: the only code that turns database rows into what pages, the API and
the CLI show, and (from PR 2) the only code that changes them.

Each page-level function takes a `Ctx` (who is asking, and when). Today there is one user, the
owner; the SaaS adds more without changing these signatures.
"""
