"""New migration steps, one module per step, so two branches that each add one never touch the same file.

Every module here is imported by ``migrations`` (see ``load_steps`` at the bottom of that file) and registers its step with
``@register(N)`` from ``..migrations``, exactly as a step in ``migrations.py`` does. Steps 1-41 stay in ``migrations.py``.
Take the next free number at merge time; ``scripts/check-migrations.py`` fails CI when two steps share a number or a name.
"""
