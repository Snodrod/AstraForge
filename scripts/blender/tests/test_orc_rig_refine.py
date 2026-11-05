"""Focused regression tests for the Orc-specific weight partition.
Run: blender -b --factory-startup --python scripts/blender/tests/test_orc_rig_refine.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from steps import _humanoid_lib as hl
from steps.rig_orc_refine import clean_orc_weights


def test_cross_limb_partition():
    names = [hl.b(n) for n in (
        "Hips", "LeftHand", "LeftForeArm", "LeftUpLeg", "LeftLeg", "LeftFoot",
        "RightHand", "RightForeArm", "RightUpLeg", "RightLeg", "RightFoot",
    )]
    joints = {
        "LeftHand": np.array([0.324, 0.058, 0.8578]),
        "LeftHandTip": np.array([0.324, 0.058, 0.6778]),
        "LeftForeArm": np.array([0.3181, 0.091, 1.1112]),
        "LeftUpLeg": np.array([0.0936, 0.0312, 0.918]),
        "LeftLeg": np.array([0.108, -0.009, 0.4957]),
        "LeftFoot": np.array([0.1206, 0.018, 0.09]),
        "RightHand": np.array([-0.349, -0.0378, 0.7436]),
        "RightHandTip": np.array([-0.3206, -0.0646, 0.6341]),
        "RightForeArm": np.array([-0.3119, -0.0012, 1.0477]),
        "RightUpLeg": np.array([-0.0936, 0.0312, 0.918]),
        "RightLeg": np.array([-0.108, -0.009, 0.4957]),
        "RightFoot": np.array([-0.1206, 0.018, 0.09]),
    }
    # 0: the observed bad leg vertex; 1: a hand vertex with leg contamination; 2: centre cloth.
    verts = np.array([
        [0.393389, 0.113, 0.412878],
        [0.324, 0.058, 0.70],
        [0.0, 0.0, 0.65],
    ])
    W = np.zeros((3, len(names)))
    W[0, names.index(hl.b("LeftHand"))] = 0.5966406
    W[0, names.index(hl.b("LeftLeg"))] = 0.3314133
    W[0, names.index(hl.b("LeftUpLeg"))] = 0.06
    W[0, names.index(hl.b("LeftFoot"))] = 0.0119461
    W[1, names.index(hl.b("LeftUpLeg"))] = 0.8
    W[1, names.index(hl.b("LeftHand"))] = 0.2
    W[2, names.index(hl.b("Hips"))] = 0.4
    W[2, names.index(hl.b("LeftUpLeg"))] = 0.3
    W[2, names.index(hl.b("RightUpLeg"))] = 0.3

    out, info = clean_orc_weights(verts, names, W, joints, {"groin_z": 0.828})

    arm = [i for i, n in enumerate(names) if hl.short(n).endswith(("Arm", "ForeArm", "Hand"))]
    assert out[0, arm].sum() < 1e-8, "leg vertex must not retain any arm influence"
    assert hl.short(names[int(np.argmax(out[0]))]) == "LeftLeg"
    assert out[1, names.index(hl.b("LeftUpLeg"))] < 1e-8, "hand must not retain thigh influence"
    assert hl.short(names[int(np.argmax(out[1]))]) == "LeftHand"
    assert np.allclose(out.sum(axis=1), 1.0), "all vertices must remain normalised"
    assert (out > 1e-4).sum(axis=1).max() <= 4
    assert out[2, names.index(hl.b("LeftUpLeg"))] > 0.0
    assert out[2, names.index(hl.b("RightUpLeg"))] > 0.0
    assert info["leg_vertices_arm_removed"] >= 1
    assert info["hand_vertices_leg_removed"] >= 1


def test_refine_step_writes_neutral_orc():
    import af_common as af
    from steps import rig_orc_refine

    root = os.path.dirname(os.path.dirname(ROOT))
    source = os.path.join(root, "data", "runtime", "orc-rig-orc-r5", "rigged.blend")
    out_dir = os.path.join(root, "data", "runtime", "orc-rig-orc-refine-test")
    req = {
        "step": "rig_orc_refine",
        "inputs": {"blend": source},
        "params": {"head_yaw_deg": 35.0, "leg_inward_deg": 5.0, "groin_z": 0.828},
        "out_dir": out_dir,
    }
    af.open_blend(source)
    result = rig_orc_refine.run(req)
    for key in ("glb", "blend", "rest_png", "poses_png", "profile_json"):
        assert os.path.isfile(result["outputs"][key]), "missing output: " + key
    assert result["stats"]["stance"]["head_yaw_deg"] == 35.0
    assert result["stats"]["stance"]["leg_inward_deg"] == 5.0
    assert result["stats"]["weights"]["unweighted_vertices"] == 0
    assert result["stats"]["bones"] == 25
    failed_gates = [c["name"] for c in result["checks"] if c["severity"] == "error" and not c["ok"]]
    assert not failed_gates, "Orc deformation gates regressed: " + ", ".join(failed_gates)
    deform = result["stats"]["deformation"]
    assert deform["arms_forward"]["torn_area_frac"] <= hl.TORN_LIMITS["main"]
    assert deform["arms_up"]["torn_area_frac"] <= hl.TORN_LIMITS["arms_up"]


if __name__ == "__main__":
    test_cross_limb_partition()
    if os.environ.get("ORC_RIG_INTEGRATION") == "1":
        test_refine_step_writes_neutral_orc()
    print("ORC_RIG_REFINE_TEST_PASS", flush=True)
