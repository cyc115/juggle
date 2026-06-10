"""
dbops — JuggleDB domain mixin package.

Owns: the focused DB modules (schema/DDL, migrations, and per-domain mixins)
assembled into JuggleDB by src/juggle_db.py, which remains the public import
surface (`from juggle_db import JuggleDB`).
Must not own: command handlers or non-DB logic.
"""
