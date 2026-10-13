"""Rewrite agent. Ports src/selfHeal/rewrite.js exactly.

Patches ONE step's `findEl([...])` call in place, rather than regenerating
the whole script — per the plan, "not a full Stage 1-2 re-run unless the
drift-check says the whole funnel changed."

This is pure string substitution, not a model call (a recent, deliberate
change from an earlier design that DID call an LLM — do not revert to that).
The new candidates already come from diagnose_drift() (drift_check.py) — the
only thing left to do with them is known precisely: splice them into the one
findEl(...) call at the known step_index. Asking a model to "echo the whole
script back with one line changed" bought nothing (the candidates were
already decided) while adding real risk: an LLM asked to reproduce a long
script byte-identical can still subtly reformat something, and because it's
re-generating the WHOLE file, that "something" could in principle land
inside the payment-safety logic (convert.py's CONTRACT §3) that this pass was
never supposed to touch at all. A targeted splice can't touch anything
outside the one line it's replacing, so that failure mode doesn't exist here.

Depends on convert.py's own CONTRACT (§9) that `<N>` in `const el<N> = ...`
is exactly the funnel recording's step index — if that ever stops holding,
this raises loudly rather than silently patching the wrong step or
corrupting the script.
"""
import json
import re
from typing import Optional


def rewrite_script(
    *,
    script: str,
    step_index: int,
    diagnosis: Optional[str] = None,
    suggested_selectors: Optional[list] = None,
) -> dict:
    """@param script - the current (failing) script source
    @param step_index
    @param diagnosis - carried through for the self-heal audit trail/log only;
      not used to patch anything
    @param suggested_selectors
    @returns {"script": str, "mocked": False}
    """
    if not isinstance(suggested_selectors, list) or len(suggested_selectors) == 0:
        raise ValueError(
            f"rewrite_script: no suggested_selectors provided for step {step_index} "
            f"(diagnosis: {diagnosis if diagnosis is not None else 'none'})"
        )

    # Matches `const el<step_index> = findEl([` ... up to the first `],` — i.e.
    # just the candidates array, stopping before the trailing `, <N>, "<action>");`
    # arguments. Lazy match is safe here: a CSS selector string containing the
    # literal sequence `],` is not a realistic case, and the "not found" check
    # below turns a genuine mismatch into a loud failure instead of a silent
    # no-op.
    pattern = re.compile(r"(const el" + str(step_index) + r" = findEl\(\[)[\s\S]*?(\],)")

    match = pattern.search(script)
    if not match:
        raise ValueError(
            f'rewrite_script: could not find "const el{step_index} = findEl([...], {step_index}, ...);" '
            "in the script. Either step numbering has drifted from the funnel recording, or this "
            "script predates the findEl contract — do not patch blind; escalate for a full "
            "re-convert instead."
        )

    # JSON.stringify(["a","b"]) => `["a","b"]` — strip the outer brackets since
    # the regex groups already capture the surrounding `findEl([` ... `]);`.
    candidates_literal = json.dumps(suggested_selectors)[1:-1]

    # Replacement via a function (not a template string) so nothing in
    # candidates_literal (e.g. a literal backslash in a selector) is ever
    # misread as a regex backreference.
    patched = pattern.sub(lambda m: f"{m.group(1)}{candidates_literal}{m.group(2)}", script, count=1)

    return {"script": patched, "mocked": False}
