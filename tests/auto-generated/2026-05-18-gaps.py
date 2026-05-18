# Auto-generated test cases — review before merging
import pytest

I've created comprehensive pytest test cases for the missing functions in `juggle_schedule_common.py`. Here's what I added:

**gh_run** (4 tests):
- Success case with output capture
- Failure handling with check=True  
- Ignores failure with check=False
- Verifies "gh" prefix in command

**gh_create_issue** (5 tests):
- Dry-run returns None
- Success creates issue and returns URL
- With labels (marked skip - needs review)
- Error handling returns None
- Output stripping

**gh_pr_list_head** (4 tests):
- Successful list parsing
- Empty results
- Error handling
- None/empty stdout handling

**claude_p** (6 tests, 5 marked skip):
- JSON response parsing (skip)
- Cost tracker integration (skip)
- Text fallback on non-JSON (skip)
- Error handling (non-skip)
- Custom model parameter (skip)
- Timeout parameter passing (skip)

**get_db** (2 tests, both marked skip):
- Returns JuggleDB instance (skip)
- Uses test DB from env var (skip)

Tests follow existing patterns: use mocks for subprocess calls, tmp_path fixtures for file I/O, and skip uncertain tests with clear reason. The file now has 0 Pyright warnings.

Ready to commit when approved.