"""Complexity and reasoning-need estimation.

Decides whether a prompt requires a reasoning-class model or can be handled by
a cheaper / free model, independent of its task type.

Architecture:
1. **Heuristics (Tier 1)**: free, instant (~0 ms), and explainable. Detects
   multi-step instructions, math/logic proofs, algorithmic code/tracebacks,
   constraint density, and counter-signals (extraction, translation, formatting).
2. **Injected LLM Judge (Tier 2)**: consulted only when heuristic confidence is
   below threshold (`heuristic_confident`). Keeps costs near zero for the clear
   70%+ of queries while handling ambiguous prompts accurately.
3. **Outcome / Escalation**: provides ground-truth feedback when a cheap model fails
   downstream validation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# Default signal weights
DEFAULT_WEIGHTS: dict[str, float] = {
    "length": 0.15,
    "multi_step": 0.45,
    "math_logic": 0.85,
    "code_debug": 0.80,
    "multi_ask": 0.30,
    "constraints": 0.25,
    "negative": -0.50,
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "low_max": 0.25,
    "high_min": 0.65,
}

DEFAULT_HEURISTIC_CONFIDENT: float = 0.75
DEFAULT_JUDGE_MAX_CHARS: int = 1500


@dataclass(frozen=True, slots=True)
class Complexity:
    """Estimated complexity and reasoning requirement of a prompt."""

    level: str  # "low" | "medium" | "high"
    needs_reasoning: bool
    confidence: float
    score: float
    signals: tuple[str, ...] = ()
    source: str = "heuristic"  # heuristic | judge | override | escalation | default
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "needs_reasoning": self.needs_reasoning,
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "signals": list(self.signals),
            "source": self.source,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Signal Detection Heuristics
# --------------------------------------------------------------------------- #

_RE_MULTI_STEP = re.compile(
    r"\b("
    r"step[ -]by[ -]step|break down|first.*?then|chain of thought|"
    r"plan and execute|workflow|multi[ -]step|subsequent steps?|"
    r"derive step|trade[ -]?offs?|pros and cons|compare\b|evaluate\b|"
    r"root cause analysis|system architecture|strategy\b|architect"
    r")\b",
    re.IGNORECASE,
)

_RE_MATH_LOGIC = re.compile(
    r"\b("
    r"prove\b|proof\b|derive\b|derivation\b|theorem|lemma|axiom|"
    r"integral|derivative|calculus|combinatorics|permutation|probability|"
    r"expected value|bayes|stochastic|matrix|eigenvalue|vector space|"
    r"induction|contradiction|syllogism|logic puzzle|truth table|"
    r"solve for [a-z]|formula for|equation"
    r")\b|"
    # Arithmetic must be digit-anchored on both sides. The previous loose form
    # treated `/` and `-` as operators, so any path or URL with two slashes
    # matched: "deploy/cloud/k8s.yaml" and "https://api.github.com/repos/x" both
    # scored HIGH and were routed to the reasoning tier — the most trivial
    # agentic steps billed as mathematics. The bare-dash chain went too: it
    # matched ISO dates.
    r"\b\d+(?:\.\d+)?(?:\s*[+*^]\s*\d+(?:\.\d+)?)+\b|"
    r"\b\d+\s*[\+\-\*\/]\s*\d+\s*=",
    re.IGNORECASE,
)

_RE_CODE_DEBUG = re.compile(
    r"(Traceback \(most recent call last\):|"
    r"NullPointerException|segmentation fault|panic:|SIGSEGV|"
    r"\b(time complexity|space complexity|big-o|dynamic programming|"
    r"np-hard|deadlock|race condition|memory leak|concurrency|mutex|"
    r"distributed consensus|raft|paxos|find the bug|debug why|fix this bug|"
    r"fix the race condition)\b)",
    re.IGNORECASE,
)

_RE_CONSTRAINTS = re.compile(
    r"\b("
    r"must|strictly|shall not|never|under no circumstances|"
    r"unless|only if|constraint|constraints|exact format|without using|"
    r"at most|at least|do not use|cannot exceed"
    r")\b",
    re.IGNORECASE,
)

_RE_NEGATIVE_TASK = re.compile(
    r"\b("
    r"extract|extract all|parse json|regex for|find emails?|get the url|"
    r"pull out|convert to json|format as markdown|pretty print|reformat|"
    r"turn this into a table|csv to json|yaml to json|json to csv|"
    r"classify|categorize|sentiment|is this spam|binary classification|"
    r"translate to|translate into|spanish|french|german|chinese|japanese|"
    r"summarize|summarise|tl;dr|tldr|brief summary|in \d+ bullets?|"
    r"what is the capital of|who wrote|what year was|define the word|"
    r"synonym for|meaning of"
    r")\b",
    re.IGNORECASE,
)


def extract_signals(prompt: str) -> dict[str, float]:
    """Identify complexity signals in text. Returns {signal_name: raw_magnitude}."""
    signals: dict[str, float] = {}
    clean = prompt.strip()
    length = len(clean)

    # 1. Length signal (scaled: 0 for < 200 chars, up to 1.0 at 2500+ chars)
    if length > 2500:
        signals["length"] = 1.0
    elif length > 600:
        signals["length"] = round((length - 600) / 1900, 2)
    elif length > 120:
        signals["length"] = 0.2

    # 2. Multi-step phrasing
    ms_matches = len(_RE_MULTI_STEP.findall(clean))
    if ms_matches:
        signals["multi_step"] = min(1.0, 0.5 + 0.25 * ms_matches)

    # 3. Math & logical reasoning
    math_matches = len(_RE_MATH_LOGIC.findall(clean))
    if math_matches:
        signals["math_logic"] = min(1.0, 0.7 + 0.15 * math_matches)

    # 4. Code debugging & algorithmic complexity
    code_matches = len(_RE_CODE_DEBUG.findall(clean))
    if code_matches or "```" in clean:
        signals["code_debug"] = min(1.0, 0.7 + 0.15 * code_matches + (0.15 if "```" in clean else 0.0))

    # 5. Multiple asks / sub-questions (e.g. numbered questions, multiple ?)
    q_count = clean.count("?")
    numbered_count = len(re.findall(r"(?:^|\n)\s*\d+[\.\)]\s+", clean))
    if numbered_count >= 2 or q_count >= 3:
        signals["multi_ask"] = min(1.0, 0.4 + 0.15 * max(numbered_count, q_count))

    # 6. Constraint density
    c_matches = len(_RE_CONSTRAINTS.findall(clean))
    if c_matches >= 1:
        signals["constraints"] = min(1.0, 0.4 + 0.2 * (c_matches - 1))

    # 7. Negative / simple task markers
    neg_matches = len(_RE_NEGATIVE_TASK.findall(clean))
    if neg_matches:
        signals["negative"] = min(1.0, 0.6 + 0.2 * neg_matches)

    return signals


def score_signals(
    prompt: str, weights: dict[str, float] | None = None
) -> tuple[float, list[str]]:
    """Compute weighted complexity score (clamped to 0.0..1.0) and active signals."""
    w = DEFAULT_WEIGHTS if weights is None else {**DEFAULT_WEIGHTS, **weights}
    extracted = extract_signals(prompt)

    total_score = 0.0
    active_signals: list[str] = []

    for name, mag in extracted.items():
        weight = w.get(name, 0.0)
        contribution = weight * mag
        total_score += contribution
        if abs(contribution) >= 0.05:
            active_signals.append(f"{name}:{mag:.1f}")

    # Clamp score to [0.0, 1.0]
    clamped_score = max(0.0, min(1.0, total_score))
    return clamped_score, active_signals


def estimate_heuristic(
    prompt: str,
    *,
    weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
) -> Complexity:
    """Fast, deterministic heuristic complexity scoring."""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    low_max = th["low_max"]
    high_min = th["high_min"]

    score, signals = score_signals(prompt, weights)

    # Determine level and reasoning need
    if score <= low_max:
        level = "low"
        needs_reasoning = False
        # Confidence increases as score approaches 0
        margin = (low_max - score) / max(0.01, low_max)
        confidence = min(1.0, 0.5 + 0.5 * margin)
        reason = "clear simple task / low cognitive load"
    elif score >= high_min:
        level = "high"
        needs_reasoning = True
        # Confidence increases as score approaches 1.0
        margin = (score - high_min) / max(0.01, 1.0 - high_min)
        confidence = min(1.0, 0.5 + 0.5 * margin)
        reason = "heavy reasoning markers detected"
    else:
        level = "medium"
        mid = (low_max + high_min) / 2.0
        needs_reasoning = score >= mid
        # Distance to closest threshold boundary indicates ambiguity
        dist_to_boundary = min(score - low_max, high_min - score)
        span = (high_min - low_max) / 2.0
        confidence = max(0.40, min(0.70, 0.40 + 0.30 * (dist_to_boundary / max(0.01, span))))
        reason = "moderate complexity or mixed signals"

    return Complexity(
        level=level,
        needs_reasoning=needs_reasoning,
        confidence=round(confidence, 3),
        score=round(score, 3),
        signals=tuple(signals),
        source="heuristic",
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# LLM Judge Rubric & Prompt
# --------------------------------------------------------------------------- #

JUDGE_SYSTEM_PROMPT = """You are a routing complexity judge. Your job is to classify whether a user prompt requires a deep reasoning model (e.g. o1/Claude Thinking/DeepSeek-R1) or can be reliably solved by a fast/standard model.

Guidelines:
- needs_reasoning=true (high/medium): multi-step deductive proofs, math calculations with multiple steps, logic puzzles, algorithm design with edge cases, root cause debugging under uncertainty, complex multi-constraint planning.
- needs_reasoning=false (low/medium): simple extraction, classification, format transformation (json/markdown/csv), language translation, summarization, direct factual lookups, standard boilerplate generation.

Output strictly valid JSON with no markdown wrapping:
{"level": "low|medium|high", "needs_reasoning": true|false, "reason": "<=15 words explaining why>"}"""

JUDGE_FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {
        "prompt": "Extract the email and phone number from this snippet.",
        "reply": '{"level": "low", "needs_reasoning": false, "reason": "straightforward information extraction"}',
    },
    {
        "prompt": "Prove by mathematical induction that 1 + 2 + ... + n = n(n+1)/2.",
        "reply": '{"level": "high", "needs_reasoning": true, "reason": "formal mathematical induction proof"}',
    },
    {
        "prompt": "Translate the following paragraph into conversational French.",
        "reply": '{"level": "low", "needs_reasoning": false, "reason": "standard linguistic translation task"}',
    },
    {
        "prompt": "We have 3 water jugs: 8L, 5L, and 3L. Find the exact sequence of pours to get 4L in the 8L jug.",
        "reply": '{"level": "high", "needs_reasoning": true, "reason": "state-space search and logic planning"}',
    },
    {
        "prompt": "Write a python function to compute the Fibonacci sequence iteratively.",
        "reply": '{"level": "low", "needs_reasoning": false, "reason": "basic textbook algorithmic implementation"}',
    },
    {
        "prompt": "Here is a multi-threaded C++ deadlock in our epoll loop. Trace the lock acquisition order across threads 1 and 2.",
        "reply": '{"level": "high", "needs_reasoning": true, "reason": "complex concurrent trace and deadlock analysis"}',
    },
]


def build_judge_messages(prompt: str, max_chars: int = DEFAULT_JUDGE_MAX_CHARS) -> list[dict[str, str]]:
    """Format messages for calling the LLM judge."""
    truncated = prompt[:max_chars].strip()
    messages: list[dict[str, str]] = [{"role": "system", "content": JUDGE_SYSTEM_PROMPT}]
    for ex in JUDGE_FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": ex["prompt"]})
        messages.append({"role": "assistant", "content": ex["reply"]})
    messages.append({"role": "user", "content": truncated})
    return messages


def parse_judge_output(text: str) -> dict[str, Any] | None:
    """Safely parse JSON response from LLM judge."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "level" in data and "needs_reasoning" in data:
            level = str(data["level"]).lower()
            if level in ("low", "medium", "high"):
                return {
                    "level": level,
                    "needs_reasoning": bool(data["needs_reasoning"]),
                    "reason": str(data.get("reason", ""))[:120],
                }
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Top-level Classification Flow
# --------------------------------------------------------------------------- #

class JudgeCache:
    """In-memory cache for judge decisions keyed by prompt sha256."""

    def __init__(self, capacity: int = 1000) -> None:
        self._cache: dict[str, Complexity] = {}
        self._capacity = capacity

    def _key(self, text: str) -> str:
        norm = " ".join(text.lower().split())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    def get(self, text: str) -> Complexity | None:
        return self._cache.get(self._key(text))

    def set(self, text: str, complexity: Complexity) -> None:
        if len(self._cache) >= self._capacity:
            # Drop oldest 10%
            keys = list(self._cache.keys())[: max(1, self._capacity // 10)]
            for k in keys:
                self._cache.pop(k, None)
        self._cache[self._key(text)] = complexity


_DEFAULT_CACHE = JudgeCache()


def classify_complexity(
    prompt: str,
    *,
    weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
    heuristic_confident: float = DEFAULT_HEURISTIC_CONFIDENT,
    judge: Callable[[str], Complexity | dict[str, Any] | str | None] | None = None,
    judge_max_chars: int = DEFAULT_JUDGE_MAX_CHARS,
    override: str | None = None,
    cache: JudgeCache | None = _DEFAULT_CACHE,
    policy_config: dict[str, Any] | None = None,
) -> Complexity:
    """Classify the complexity of a prompt.

    1. If `override` is supplied, honours it immediately.
    2. Runs deterministic heuristics.
    3. If heuristic confidence >= heuristic_confident, returns heuristic result.
    4. Otherwise, invokes `judge` tie-break if provided. On failure/timeout,
       safely returns the heuristic estimate.
    """
    if policy_config:
        if weights is None and "weights" in policy_config:
            weights = policy_config.get("weights")
        if thresholds is None and "thresholds" in policy_config:
            thresholds = policy_config.get("thresholds")
        if "heuristic_confident" in policy_config:
            heuristic_confident = float(policy_config["heuristic_confident"])
        if "judge" in policy_config and isinstance(policy_config["judge"], dict):
            jconf = policy_config["judge"]
            if "max_chars" in jconf:
                judge_max_chars = int(jconf["max_chars"])
    if override in ("low", "medium", "high"):
        needs_reasoning = override == "high"
        return Complexity(
            level=override,
            needs_reasoning=needs_reasoning,
            confidence=1.0,
            score=0.0 if override == "low" else (0.5 if override == "medium" else 1.0),
            signals=("override",),
            source="override",
            reason=f"caller forced complexity={override}",
        )

    heuristic = estimate_heuristic(prompt, weights=weights, thresholds=thresholds)

    # If heuristic is confident enough or no judge is available, we're done
    if heuristic.confidence >= heuristic_confident or judge is None:
        return heuristic

    # Check judge cache
    if cache is not None:
        cached = cache.get(prompt)
        if cached is not None:
            return cached

    # Consult judge
    try:
        sample = prompt[:judge_max_chars]
        raw_res = judge(sample)
        if raw_res is None:
            return heuristic

        if isinstance(raw_res, Complexity):
            res = raw_res
        elif isinstance(raw_res, dict) and "level" in raw_res:
            res = Complexity(
                level=str(raw_res["level"]).lower(),
                needs_reasoning=bool(raw_res.get("needs_reasoning", False)),
                confidence=float(raw_res.get("confidence", 0.90)),
                score=heuristic.score,
                signals=heuristic.signals,
                source="judge",
                reason=str(raw_res.get("reason", "classified by llm judge")),
            )
        elif isinstance(raw_res, str):
            parsed = parse_judge_output(raw_res)
            if parsed:
                res = Complexity(
                    level=parsed["level"],
                    needs_reasoning=parsed["needs_reasoning"],
                    confidence=0.90,
                    score=heuristic.score,
                    signals=heuristic.signals,
                    source="judge",
                    reason=parsed["reason"],
                )
            else:
                return heuristic
        else:
            return heuristic

        if cache is not None:
            cache.set(prompt, res)
        return res
    except Exception:
        # Never fail a route due to judge exception
        return heuristic


def adapt_task_for_complexity(
    task: Any,
    complexity: Complexity,
    *,
    floor_delta: float = 0.08,
) -> Any:
    """Adapt a task profile based on detected prompt complexity.

    - `needs_reasoning` raises the quality floor by `floor_delta` — an *additive*
      step, so it still bites on tasks that already floor at 0.55-0.6. An absolute
      `reasoning_floor` sat *below* every demanding task and did nothing there,
      and only moved the easy task.
    - Difficulty never adds a capability. Gating on `require["reasoning"]` made a
      heuristic guess a hard capability fact, emptied the pool on tasks with
      `on_unverified_capability: reject`, and the fallback then silently discarded
      the whole adaptation. Capability stays task-driven; difficulty moves the bar.
    - `level == 'low'` scales down the token budget for a minimal footprint.
    - `level == 'medium'` preserves the base profile.
    """
    req = dict(getattr(task, "require", {}))
    tin = getattr(task, "tokens_in", 1000)
    tout = getattr(task, "tokens_out", 400)
    floor = getattr(task, "min_success_lb", 0.0)
    desc = getattr(task, "description", "")

    if complexity.needs_reasoning:
        floor = min(0.95, floor + floor_delta)
        desc = f"{desc} [reasoning_required]"
    elif complexity.level == "low":
        tin = min(tin, 1500)
        tout = min(tout, 500)
        desc = f"{desc} [low_complexity]"

    # Construct same class type as task
    cls = task.__class__
    return cls(
        name=task.name,
        tokens_in=tin,
        tokens_out=tout,
        require=req,
        min_context=getattr(task, "min_context", 0),
        min_success_lb=floor,
        benchmark_keys=getattr(task, "benchmark_keys", ()),
        benchmark_weights=getattr(task, "benchmark_weights", {}),
        cues=getattr(task, "cues", ()),
        description=desc,
        on_unverified_capability=getattr(task, "on_unverified_capability", None),
    )


def make_judge(
    config: dict[str, Any] | None,
    *,
    runner_factory: Callable[[], Any] | None = None,
) -> Callable[[str], str | None] | None:
    """Build the Tier-2 judge callable from the `complexity.judge` policy block.

    The call is **pinned** to `config['model']` — a `provider:model` deployment id —
    so it never re-enters the router: a judge that were routed would have its own
    prompt classified, possibly escalated, and would recurse. `runner_factory`
    lets a test inject a stub, and the network import is deferred until the tier is
    actually enabled, so this module stays import-light.

    Returns `None` when the tier is disabled or unconfigured, so the caller keeps
    the zero-latency heuristic path (the default: `judge.enabled: false`).
    """
    cfg = config or {}
    if not cfg.get("enabled"):
        return None
    model = cfg.get("model")
    if not model:
        return None
    timeout = float(cfg.get("timeout_s", 2.0))
    max_chars = int(cfg.get("max_chars", DEFAULT_JUDGE_MAX_CHARS))
    max_tokens = int(cfg.get("max_tokens", 60))

    def ask(prompt: str) -> str | None:
        try:
            if runner_factory is not None:
                runner = runner_factory()
            else:
                from .execute import Runner  # lazy: keeps this module import-light
                runner = Runner(timeout=timeout, temperature=0.0, max_tokens=max_tokens)
            res = runner(model, build_judge_messages(prompt, max_chars=max_chars))
            if not getattr(res, "ok", False):
                return None
            text = getattr(res, "text", None)
            return text if text and text.strip() else None
        except Exception:
            # A judge that fails, times out or answers unparseably must never
            # change the route — only the label. The heuristic stands.
            return None

    return ask


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #

#: Hand-labelled prompts for choosing the thresholds. Small and obvious on
#: purpose: the claim under test is not "the classifier is accurate" but "the cut
#: points were chosen from data rather than because 0.25 and 0.65 look reasonable".
CALIBRATION_SET: tuple[tuple[str, str], ...] = (
    # --- low ---------------------------------------------------------------
    ("Extract all email addresses from this text", "low"),
    ("Translate this paragraph into French", "low"),
    ("Format this list as markdown bullets", "low"),
    ("Summarise the article in three bullets", "low"),
    ("What is the capital of Japan?", "low"),
    ("Classify the sentiment of this review", "low"),
    ("Convert this CSV to JSON", "low"),
    # Paths and URLs are the regression case: they must not read as mathematics.
    ("Read mininfer/proxy.py and deploy/cloud/k8s.yaml and list what they do", "low"),
    # --- high --------------------------------------------------------------
    ("Prove by induction that sum(i^2) = n(n+1)(2n+1)/6", "high"),
    ("Derive the closed form of the Fibonacci recurrence", "high"),
    ("Trace the deadlock in this multithreaded epoll loop", "high"),
    ("Find the race condition in this mutex acquisition order", "high"),
    ("Design an algorithm for the travelling salesman with edge constraints", "high"),
    ("Solve the differential equation dy/dx = y with y(0) = 1", "high"),
    ("Analyse the trade-offs of Raft versus Paxos under partitions", "high"),
    ("What is 12 * 7 + 5?", "high"),
    # --- medium (the ambiguous band the judge exists for) ------------------
    ("Compare the trade-offs of these two approaches in detail", "medium"),
    ("Evaluate the architecture and outline a strategy", "medium"),
    ("First refactor the module, then update the tests", "medium"),
    ("Plan and execute a migration across three regions", "medium"),
    ("Summarise the changes in config/policy.yaml and docs/api-contract.md", "medium"),
)


def calibrate(*, weights: dict[str, float] | None = None,
              thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """Accuracy and confusion of the heuristic over `CALIBRATION_SET`."""
    labels = ("low", "medium", "high")
    confusion = {want: {got: 0 for got in labels} for want in labels}
    correct = 0
    for prompt, want in CALIBRATION_SET:
        got = estimate_heuristic(prompt, weights=weights, thresholds=thresholds).level
        confusion[want][got] += 1
        correct += int(got == want)
    total = len(CALIBRATION_SET)
    return {"n": total, "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0.0,
            "confusion": confusion}


def sweep_thresholds(
    *, weights: dict[str, float] | None = None,
    low_grid: list[float] | None = None,
    high_grid: list[float] | None = None,
    prefer: tuple[float, float] = (0.25, 0.65),
) -> list[dict[str, Any]]:
    """Every ordered `(low_max, high_min)` pair, best accuracy first.

    Ties break toward `prefer`, so a one-sample difference cannot move the shipped
    cut points into a corner of the grid.
    """
    lows = low_grid or [round(0.10 + 0.05 * i, 2) for i in range(7)]      # .10 … .40
    highs = high_grid or [round(0.45 + 0.05 * i, 2) for i in range(8)]    # .45 … .80
    out: list[dict[str, Any]] = []
    for lo in lows:
        for hi in highs:
            if hi <= lo:
                continue
            r = calibrate(weights=weights, thresholds={"low_max": lo, "high_min": hi})
            r["low_max"], r["high_min"] = lo, hi
            r["distance"] = abs(lo - prefer[0]) + abs(hi - prefer[1])
            out.append(r)
    out.sort(key=lambda r: (-r["accuracy"], r["distance"]))
    return out

