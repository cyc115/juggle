"""Pure prompt-stamp helpers (2026-06-30 orchestration-metrics Task 2)."""
import juggle_prompt_metrics as pm


def test_fingerprint_stable_and_short():
    assert pm.prompt_fingerprint("hello") == pm.prompt_fingerprint("hello")
    assert len(pm.prompt_fingerprint("hello")) == 12
    assert pm.prompt_fingerprint("a") != pm.prompt_fingerprint("b")


def test_prompt_bytes():
    assert pm.prompt_bytes_of("abc") == 3
    assert pm.prompt_bytes_of("é") == 2


def test_boilerplate_bytes_measures_prefix():
    preamble = "PREAMBLE\n"
    full = preamble + "## Working Directory\ncd x\n---\n\nreal user task"
    bp = pm.boilerplate_bytes(full, preamble=preamble)
    assert bp == len((preamble + "## Working Directory\ncd x\n---\n\n").encode("utf-8"))


def test_boilerplate_bytes_zero_when_no_marker():
    assert pm.boilerplate_bytes("just a bare prompt", preamble="PRE") == 0
