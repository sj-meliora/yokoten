# Agent operating rules

## Source integration branch must be confirmed

When a request requires `resolve_sha.py`, do not infer the source integration
branch from examples, repository names, existing remote refs, or prior tasks.

- If the user has not explicitly identified the source branch, ask whether it is
  `develop` or the exact `develop_XXX` branch **before** running Git discovery,
  fetch, or `resolve_sha.py`.
- Do not guess `develop_Evan`, search for a likely `develop_*` branch, or fetch a
  guessed branch to avoid asking.
- If the user already supplied an unambiguous branch, do not ask again. Pass its
  remote-tracking ref (for example, `origin/develop` or
  `origin/develop_XXX`) to `--branch`.

