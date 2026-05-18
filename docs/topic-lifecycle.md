---

Found 1 drift: **Default TTL is 1 hour, not 24 hours.**

The doc stated `settings.thread_auto_archive_ttl_secs = '86400'` (24h), but the actual default seeded in `juggle_db.py:379` is `'3600'` (1 hour).

Fixed and committed.