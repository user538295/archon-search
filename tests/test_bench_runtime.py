"""Pure parts of scripts/bench_runtime.py: the workload generator, the model list and the compare gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from archon_search.model_specs import EMBEDDER_SPECS
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_runtime.py"
ALL_MODELS = [
    *dict.fromkeys(
        p.embedder
        for p in [*ENGLISH_PROFILES.values(), *MULTILINGUAL_PROFILES.values()]
    ),
    *dict.fromkeys(
        p.reranker
        for p in [*ENGLISH_PROFILES.values(), *MULTILINGUAL_PROFILES.values()]
        if p.reranker is not None
    ),
]
BASELINE_THROUGHPUT = 100.0
PINNED_PASSAGE_PREFIX = "account request sailor village budget between"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_runtime", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = (
        module  # @dataclass resolves string annotations through sys.modules
    )
    spec.loader.exec_module(module)
    return module


bench = _load_script()


def _runtime_json(runtime: str, throughputs: dict[str, float]) -> dict:
    return {
        "runtime": runtime,
        "models": {
            name: {
                "kind": "embedder" if name in EMBEDDER_SPECS else "reranker",
                "throughput": value,
                "unit": "passages/s" if name in EMBEDDER_SPECS else "pairs/s",
            }
            for name, value in throughputs.items()
        },
    }


def _uniform(value: float) -> dict[str, float]:
    return dict.fromkeys(ALL_MODELS, value)


def _write_pair(tmp_path: Path, torch: dict, fastembed: dict) -> tuple[str, str]:
    torch_path, fastembed_path = tmp_path / "torch.json", tmp_path / "fastembed.json"
    torch_path.write_text(json.dumps(torch))
    fastembed_path.write_text(json.dumps(fastembed))
    return str(torch_path), str(fastembed_path)


def _compare(tmp_path: Path, torch: dict, fastembed: dict) -> int:
    return bench.main(["compare", *_write_pair(tmp_path, torch, fastembed)])


def test_report_marks_regression_below_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fastembed = _runtime_json("fastembed", _uniform(BASELINE_THROUGHPUT))
    at_threshold = _uniform(BASELINE_THROUGHPUT * bench.MIN_TORCH_TO_FASTEMBED_RATIO)
    assert _compare(tmp_path, _runtime_json("torch", at_threshold), fastembed) == 0
    assert "FAIL" not in capsys.readouterr().out

    slow_model = ALL_MODELS[3]
    just_below = {**at_threshold, slow_model: at_threshold[slow_model] - 0.01}
    assert _compare(tmp_path, _runtime_json("torch", just_below), fastembed) == 1
    failing_rows = [
        line for line in capsys.readouterr().out.splitlines() if "FAIL" in line
    ]
    assert len(failing_rows) == 1
    assert slow_model in failing_rows[0]


def test_faster_torch_passes(tmp_path: Path) -> None:
    fastembed = _runtime_json("fastembed", _uniform(BASELINE_THROUGHPUT))
    assert _compare(tmp_path, _runtime_json("torch", _uniform(250.0)), fastembed) == 0


def test_report_lists_all_ten_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _compare(
            tmp_path,
            _runtime_json("torch", _uniform(120.0)),
            _runtime_json("fastembed", _uniform(BASELINE_THROUGHPUT)),
        )
        == 0
    )
    table = capsys.readouterr().out
    assert len(ALL_MODELS) == 10
    for name in ALL_MODELS:
        assert name in table


def test_report_shows_both_throughputs_and_ratio(
    capsys: pytest.CaptureFixture[str],
) -> None:
    torch = _runtime_json("torch", {**_uniform(100.0), ALL_MODELS[0]: 75.0})
    fastembed = _runtime_json("fastembed", {**_uniform(100.0), ALL_MODELS[0]: 50.0})
    rows = bench.build_rows(torch, fastembed)
    row = next(r for r in rows if r.model == ALL_MODELS[0])
    assert (row.fastembed, row.torch, row.ratio) == (50.0, 75.0, 1.5)
    assert "1.50" in bench.render_markdown(rows)


def test_missing_model_is_an_error_naming_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = ALL_MODELS[7]
    torch = _runtime_json(
        "torch", {k: v for k, v in _uniform(100.0).items() if k != missing}
    )
    fastembed = _runtime_json("fastembed", _uniform(100.0))
    assert _compare(tmp_path, torch, fastembed) != 0
    assert missing in capsys.readouterr().err

    assert _compare(tmp_path, fastembed, torch) != 0
    assert missing in capsys.readouterr().err


def test_extra_model_in_one_file_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    torch = _runtime_json("torch", _uniform(100.0))
    fastembed = _runtime_json(
        "fastembed", {**_uniform(100.0), "acme/not-a-model": 100.0}
    )
    assert _compare(tmp_path, torch, fastembed) != 0
    assert "acme/not-a-model" in capsys.readouterr().err


def test_zero_baseline_throughput_is_an_error(tmp_path: Path) -> None:
    fastembed = _runtime_json("fastembed", {**_uniform(100.0), ALL_MODELS[0]: 0.0})
    assert _compare(tmp_path, _runtime_json("torch", _uniform(100.0)), fastembed) != 0


def test_benchmarked_models_are_exactly_the_models_of_the_six_profiles() -> None:
    profiles = [*ENGLISH_PROFILES.values(), *MULTILINGUAL_PROFILES.values()]
    assert len(profiles) == 6
    embedders = {p.embedder for p in profiles}
    rerankers = {p.reranker for p in profiles if p.reranker is not None}
    assert len(embedders | rerankers) == 10
    assert {
        n for n, kind in bench.MODEL_KINDS.items() if kind == "embedder"
    } == embedders
    assert {
        n for n, kind in bench.MODEL_KINDS.items() if kind == "reranker"
    } == rerankers
    assert len(bench.MODEL_KINDS) == 10


def test_passage_workload_is_deterministic_and_sized() -> None:
    passages = bench.build_passages()
    assert passages == bench.build_passages()
    assert len(passages) == 32
    assert len(set(passages)) > 1
    for passage in passages:
        assert 60 <= len(passage.split()) <= 200


def test_pair_workload_is_deterministic_and_sized() -> None:
    pairs = bench.build_pairs()
    assert pairs == bench.build_pairs()
    assert len(pairs) == 45
    for query, passage in pairs:
        assert query
        assert 60 <= len(passage.split()) <= 200


def test_passage_workload_is_pinned_across_platforms() -> None:
    # random.Random(seed) is specified to be stable across platforms and releases; a pinned
    # prefix catches an accidental seed or vocabulary change that would make runs incomparable.
    assert bench.build_passages()[0].startswith(PINNED_PASSAGE_PREFIX)


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("bge-small", ["BAAI/bge-small-en-v1.5"]),
        ("BAAI/bge-reranker-base", ["BAAI/bge-reranker-base"]),
        (
            "bge-small,jina-reranker",
            ["BAAI/bge-small-en-v1.5", "jinaai/jina-reranker-v2-base-multilingual"],
        ),
        ("ms-marco-MiniLM-L-6", ["Xenova/ms-marco-MiniLM-L-6-v2"]),
    ],
)
def test_models_filter_resolves_names_and_substrings(
    selector: str, expected: list[str]
) -> None:
    assert bench.select_models(selector) == expected


def test_models_filter_none_selects_all() -> None:
    assert sorted(bench.select_models(None)) == sorted(ALL_MODELS)


@pytest.mark.parametrize("selector", ["no-such-model", "bge", "multilingual"])
def test_models_filter_rejects_unknown_and_ambiguous(selector: str) -> None:
    with pytest.raises(bench.BenchError, match=selector):
        bench.select_models(selector)


class _FakeClock:
    """A clock the fake runners advance: each call of a runner costs the next duration of its script."""

    def __init__(self) -> None:
        self.now = 0.0
        self.calls: list[str] = []

    def runner(self, label: str, durations: list[float]):
        script = iter(durations)

        def run() -> None:
            self.calls.append(label)
            self.now += next(script)

        return run


def test_timed_runs_are_ten() -> None:
    assert bench.TIMED_RUNS == 10


def test_throughput_is_best_of_timed_runs_after_one_warmup() -> None:
    clock = _FakeClock()
    durations = [99.0, 4.0, 2.0, *[8.0] * (bench.TIMED_RUNS - 2)]
    run = clock.runner("t", durations)
    assert bench.throughput(run, 10, clock=lambda: clock.now) == 10 / 2.0
    assert len(clock.calls) == bench.TIMED_RUNS + 1


def test_interleaved_alternates_runtimes_each_round_after_one_warmup_each() -> None:
    clock = _FakeClock()
    rounds = bench.TIMED_RUNS
    runs = {
        "torch": clock.runner("t", [1.0] * (rounds + 1)),
        "fastembed": clock.runner("f", [1.0] * (rounds + 1)),
    }
    bench.interleaved_throughputs(runs, 5, clock=lambda: clock.now)
    assert clock.calls[:2] == ["t", "f"]  # warm-ups
    timed = clock.calls[2:]
    assert len(timed) == 2 * rounds
    for start in range(0, len(timed), 2):  # every round runs each runtime exactly once
        assert sorted(timed[start : start + 2]) == ["f", "t"]
    assert (
        timed[0] != timed[2]
    )  # the order flips between rounds: neither side always goes first


def test_interleaved_load_burst_hitting_both_runtimes_leaves_the_ratio_intact() -> None:
    clock = _FakeClock()
    burst = 5.0  # a load spike slows whatever runs in the same round, in either runtime
    torch = [9.0, 1.0 * burst, 0.5, *[1.0] * (bench.TIMED_RUNS - 2)]
    fastembed = [9.0, 0.8 * burst, 0.4, *[0.8] * (bench.TIMED_RUNS - 2)]
    result = bench.interleaved_throughputs(
        {"torch": clock.runner("t", torch), "fastembed": clock.runner("f", fastembed)},
        10,
        clock=lambda: clock.now,
    )
    assert result == pytest.approx({"torch": 10 / 0.5, "fastembed": 10 / 0.4})


def test_both_mode_returns_a_result_per_runtime_and_compare_accepts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build(name, kind, passages, pairs):
        return lambda: None

    monkeypatch.setattr(bench, "_RUNNERS", {"torch": build, "fastembed": build})
    monkeypatch.setattr(
        bench,
        "interleaved_throughputs",
        lambda runs, items, **_: {"torch": 90.0, "fastembed": 100.0},
    )
    monkeypatch.setattr(bench, "environment", lambda runtime: {"runtime": runtime})
    result = bench.run_benchmark("both", list(bench.MODEL_KINDS))
    assert result["runtime"] == "both"
    assert result["torch"]["runtime"] == "torch"
    assert result["fastembed"]["models"][ALL_MODELS[0]]["throughput"] == 100.0
    path = tmp_path / "both.json"
    path.write_text(json.dumps(result))
    assert bench.main(["compare", str(path)]) == 0  # 0.90 is exactly the threshold
    result["torch"]["models"][ALL_MODELS[2]]["throughput"] = 89.99
    path.write_text(json.dumps(result))
    assert bench.main(["compare", str(path)]) == 1
