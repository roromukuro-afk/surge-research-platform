"""Evaluation-only code (docs/specs/jev-evaluation-design.md).

Nothing in the production path imports this package, and nothing here writes to
a database: evaluation results live in immutable files outside the repository,
every row marked ``teacher_admissible = false`` (D-163, D-270).
"""
