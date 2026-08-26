import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "server" / "live_sync_delta.py"
SPEC = importlib.util.spec_from_file_location("live_sync_delta", MODULE_PATH)
live_sync_delta = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(live_sync_delta)


def animation(frames, duration=10):
    return {
        "t": duration,
        "export_info": {"fps": 30, "time_unit": "frames"},
        "kfs": frames,
    }


def frames(count, changed_time=None):
    return [
        {
            "t": time,
            "kf": {"Bone": [99 if time == changed_time else time] * 12},
        }
        for time in range(count)
    ]


def test_changed_keyframe_round_trip():
    base = animation(frames(10))
    current = animation(frames(10, changed_time=4))

    delta = live_sync_delta.create_delta(base, current)

    assert delta is not None
    assert [frame["t"] for frame in delta["upsert"]] == [4]
    assert live_sync_delta.apply_delta(base, delta) == current


def test_removed_keyframe_round_trip_and_stale_base_rejection():
    base = animation(frames(10))
    current = animation([frame for frame in frames(10) if frame["t"] != 7])
    delta = live_sync_delta.create_delta(base, current)

    assert delta is not None
    assert delta["remove"] == [7.0]
    assert live_sync_delta.apply_delta(base, delta) == current
    assert live_sync_delta.apply_delta(animation(frames(9)), delta) is None


def test_structural_changes_require_full_sync():
    base = animation(frames(10))
    changed_duration = animation(frames(10), duration=11)

    assert live_sync_delta.create_delta(base, changed_duration) is None


def test_generated_baked_keys_can_be_added_changed_and_removed():
    base = animation(
        [
            {"t": 0.0, "kf": {"Bone": [0] * 12}},
            {"t": 0.5, "kf": {"Bone": [5] * 12}},
            {"t": 1.0, "kf": {"Bone": [10] * 12}},
        ]
    )
    current = animation(
        [
            {"t": 0.0, "kf": {"Bone": [0] * 12}},
            {"t": 0.25, "kf": {"Bone": [3] * 12}},
            {"t": 0.5, "kf": {"Bone": [6] * 12}},
            {"t": 1.0, "kf": {"Bone": [10] * 12}},
        ]
    )

    delta = live_sync_delta.create_delta(base, current)

    assert delta is not None
    assert [frame["t"] for frame in delta["upsert"]] == [0.25, 0.5]
    assert live_sync_delta.apply_delta(base, delta) == current

    reverse = live_sync_delta.create_delta(current, base)
    assert reverse is not None
    assert reverse["remove"] == [0.25]
    assert live_sync_delta.apply_delta(current, reverse) == base
