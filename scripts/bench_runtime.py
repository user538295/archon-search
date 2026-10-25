#!/usr/bin/env python
"""CPU throughput gate for ADR 18: the PyTorch runtime against the fastembed baseline, on the ten profile models.

    bench_runtime.py --runtime torch [--models bge-small,jina-reranker] [--output torch.json]
    bench_runtime.py --runtime fastembed --output fastembed.json    # needs `uv run --with fastembed==0.9.0`
    bench_runtime.py compare torch.json fastembed.json              # markdown table, exit 1 on a regression

One invocation measures one runtime on CPU. Each model gets one warm-up run (which includes the lazy
weight load), then the best of three timed runs; embedders are measured in passages/s, rerankers in
pairs/s. The workload is generated from a fixed seed and a built-in vocabulary, so both runtimes and
every platform see identical texts. The gate fails when any model's torch throughput falls below
`MIN_TORCH_TO_FASTEMBED_RATIO` times fastembed's; exactly that ratio passes.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import random
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES

EMBEDDER = "embedder"
RERANKER = "reranker"
UNITS = {EMBEDDER: "passages/s", RERANKER: "pairs/s"}

_PROFILES = [*ENGLISH_PROFILES.values(), *MULTILINGUAL_PROFILES.values()]
# The ten models the six install profiles ship (the registries hold more); embedders first.
MODEL_KINDS: Mapping[str, str] = {
    **dict.fromkeys((p.embedder for p in _PROFILES), EMBEDDER),
    **dict.fromkeys(
        (p.reranker for p in _PROFILES if p.reranker is not None), RERANKER
    ),
}

TORCH = "torch"
FASTEMBED = "fastembed"
RUNTIMES = (TORCH, FASTEMBED)
CPU_DEVICE = "cpu"
FASTEMBED_PROVIDERS = ["CPUExecutionProvider"]

MIN_TORCH_TO_FASTEMBED_RATIO = 0.9
TIMED_RUNS = 3

WORKLOAD_SEED = 20261025
PASSAGE_COUNT = 32
PAIR_COUNT = 45
MIN_PASSAGE_WORDS = 60
MAX_PASSAGE_WORDS = 200
MIN_QUERY_WORDS = 4
MAX_QUERY_WORDS = 10

VOCABULARY = (  # noqa: SIM905 — a word list reads better as prose
    "archive search index query vector model token passage ranking document cluster memory latency "
    "throughput batch queue schema record field network server client request response cache storage "
    "engine parser pipeline stream buffer thread process kernel signal packet router gateway account "
    "invoice payment ledger report policy review budget forecast season harvest river mountain forest "
    "garden window bridge station harbor market village castle library museum theatre concert painter "
    "writer teacher doctor sailor farmer baker weaver driver pilot student winter summer morning evening "
    "quickly slowly carefully together between during against without because although whenever "
    "quiet bright ancient modern simple complex narrow broad heavy gentle remote familiar"
).split()


class BenchError(Exception):
    """A user-facing failure (bad filter, inconsistent result files, unsupported runtime model)."""


# ---------------------------------------------------------------------------------------------- workload


def _words(rng: random.Random, count: int) -> str:
    return " ".join(rng.choices(VOCABULARY, k=count))


def build_passages() -> list[str]:
    """`PASSAGE_COUNT` deterministic passages of 60-200 words."""
    rng = random.Random(WORKLOAD_SEED)
    return [
        _words(rng, rng.randint(MIN_PASSAGE_WORDS, MAX_PASSAGE_WORDS))
        for _ in range(PASSAGE_COUNT)
    ]


def build_pairs() -> list[tuple[str, str]]:
    """`PAIR_COUNT` deterministic (short query, 60-200 word passage) pairs."""
    rng = random.Random(WORKLOAD_SEED + 1)
    return [
        (
            _words(rng, rng.randint(MIN_QUERY_WORDS, MAX_QUERY_WORDS)),
            _words(rng, rng.randint(MIN_PASSAGE_WORDS, MAX_PASSAGE_WORDS)),
        )
        for _ in range(PAIR_COUNT)
    ]


# ------------------------------------------------------------------------------------------ model filter


def select_models(selector: str | None) -> list[str]:
    """Registry names matching *selector* (comma-separated names or unambiguous substrings), registry order."""
    if selector is None:
        return list(MODEL_KINDS)
    chosen: set[str] = set()
    for token in (part.strip() for part in selector.split(",")):
        matches = [name for name in MODEL_KINDS if name == token] or [
            name for name in MODEL_KINDS if token.lower() in name.lower()
        ]
        if len(matches) != 1:
            kind = (
                "matches no model"
                if not matches
                else f"is ambiguous: {', '.join(matches)}"
            )
            raise BenchError(f"--models entry {token!r} {kind}")
        chosen.add(matches[0])
    return [name for name in MODEL_KINDS if name in chosen]


# ----------------------------------------------------------------------------------------------- running


def throughput(run: Callable[[], object], items: int) -> float:
    """Items per second of the fastest of `TIMED_RUNS` runs after one warm-up run."""
    run()
    best = 0.0
    for _ in range(TIMED_RUNS):
        started = time.perf_counter()
        run()
        best = max(best, items / (time.perf_counter() - started))
    return best


def _torch_runner(
    name: str, kind: str, passages: list[str], pairs: list[tuple[str, str]]
) -> Callable[[], object]:
    if kind == EMBEDDER:
        from archon_search.embedder import ModelEmbedder

        embedder = ModelEmbedder(name, device=CPU_DEVICE)
        return lambda: embedder.encode(passages)
    from archon_search.reranker import ModelReranker

    reranker = ModelReranker(name, device=CPU_DEVICE)
    return lambda: reranker.predict(pairs)


def _fastembed_runner(
    name: str, kind: str, passages: list[str], pairs: list[tuple[str, str]]
) -> Callable[[], object]:
    # Both registries use fastembed's own model names (verified against 0.9.0's list_supported_models()).
    try:
        if kind == EMBEDDER:
            from fastembed import TextEmbedding

            embedder = TextEmbedding(name, providers=FASTEMBED_PROVIDERS)
            return lambda: list(embedder.embed(passages))
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        reranker = TextCrossEncoder(name, providers=FASTEMBED_PROVIDERS)
        return lambda: list(reranker.rerank_pairs(pairs))
    except (
        ValueError
    ) as error:  # fastembed raises ValueError for a model it does not list
        raise BenchError(f"fastembed does not support {name}: {error}") from error


_RUNNERS = {TORCH: _torch_runner, FASTEMBED: _fastembed_runner}


def environment(runtime: str) -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "torch": metadata.version("torch"),
        "fastembed": metadata.version("fastembed") if runtime == FASTEMBED else None,
    }


def run_benchmark(runtime: str, names: Sequence[str]) -> dict[str, Any]:
    passages, pairs = build_passages(), build_pairs()
    models: dict[str, dict[str, Any]] = {}
    for name in names:
        kind = MODEL_KINDS[name]
        print(f"[bench] {runtime} {name}", file=sys.stderr, flush=True)
        run = _RUNNERS[runtime](name, kind, passages, pairs)
        models[name] = {
            "kind": kind,
            "throughput": throughput(
                run, len(passages) if kind == EMBEDDER else len(pairs)
            ),
            "unit": UNITS[kind],
        }
        del run
        gc.collect()
    return {"runtime": runtime, "environment": environment(runtime), "models": models}


# ------------------------------------------------------------------------------------------------ compare


@dataclass(frozen=True)
class Row:
    model: str
    kind: str
    fastembed: float
    torch: float

    @property
    def ratio(self) -> float:
        return self.torch / self.fastembed

    @property
    def passed(self) -> bool:
        return self.ratio >= MIN_TORCH_TO_FASTEMBED_RATIO


def _check_models(label: str, result: Mapping[str, Any]) -> list[str]:
    models = result.get("models")
    if not isinstance(models, dict):
        return [f"{label}: no 'models' object"]
    problems = [
        f"{label}: model {name} is missing"
        for name in MODEL_KINDS
        if name not in models
    ]
    problems += [
        f"{label}: unexpected model {name}"
        for name in models
        if name not in MODEL_KINDS
    ]
    for name in MODEL_KINDS.keys() & models.keys():
        value = (
            models[name].get("throughput") if isinstance(models[name], dict) else None
        )
        if not isinstance(value, int | float) or value <= 0:
            problems.append(
                f"{label}: model {name} has no positive throughput (got {value!r})"
            )
    return problems


def build_rows(torch: Mapping[str, Any], fastembed: Mapping[str, Any]) -> list[Row]:
    """One row per registry model; raises `BenchError` listing every missing, extra or unusable entry."""
    problems = _check_models("torch", torch) + _check_models("fastembed", fastembed)
    if problems:
        raise BenchError("; ".join(problems))
    return [
        Row(
            name,
            kind,
            float(fastembed["models"][name]["throughput"]),
            float(torch["models"][name]["throughput"]),
        )
        for name, kind in MODEL_KINDS.items()
    ]


def render_markdown(rows: Sequence[Row]) -> str:
    lines = [
        f"### PyTorch vs fastembed, CPU (gate: torch >= {MIN_TORCH_TO_FASTEMBED_RATIO:.2f} x fastembed)",
        "",
        "| Model | Kind | fastembed | torch | torch / fastembed | Result |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        unit = UNITS[row.kind]
        lines.append(
            f"| {row.model} | {row.kind} | {row.fastembed:.2f} {unit} | {row.torch:.2f} {unit} "
            f"| {row.ratio:.2f} | {'PASS' if row.passed else 'FAIL'} |"
        )
    return "\n".join(lines) + "\n"


def compare(torch_path: Path, fastembed_path: Path) -> int:
    rows = build_rows(
        json.loads(torch_path.read_text(encoding="utf-8")),
        json.loads(fastembed_path.read_text(encoding="utf-8")),
    )
    print(render_markdown(rows), end="")
    return 0 if all(row.passed for row in rows) else 1


# ------------------------------------------------------------------------------------------------- main


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    if argv and argv[0] == "compare":
        parser = argparse.ArgumentParser(prog="bench_runtime.py compare")
        parser.add_argument("command", choices=["compare"])
        parser.add_argument("torch_json", type=Path)
        parser.add_argument("fastembed_json", type=Path)
        return parser.parse_args(argv)
    parser = argparse.ArgumentParser(
        prog="bench_runtime.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument(
        "--models", help="comma-separated registry names or unambiguous substrings"
    )
    parser.add_argument(
        "--output", type=Path, help="write the JSON here instead of stdout"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if getattr(args, "command", None) == "compare":
            return compare(args.torch_json, args.fastembed_json)
        result = (
            json.dumps(
                run_benchmark(args.runtime, select_models(args.models)), indent=2
            )
            + "\n"
        )
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.output is None:
        print(result, end="")
    else:
        args.output.write_text(result, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
