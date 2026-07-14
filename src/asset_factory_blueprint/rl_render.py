"""Render a validated RL contract as an Isaac Lab configuration package."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from asset_factory_blueprint.execution import durable_replace, durable_unlink, workspace_lease
from asset_factory_blueprint.rl_evidence import environment_contract_sha256, environment_manifest_sha256
from asset_factory_blueprint.services.rl_environment import SUPPORTED_REWARDS
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint

RENDERER_ID = "asset-factory.rl-renderer"
RENDERER_VERSION = "1.0"
SUPPORTED_POLICY_OBSERVATIONS = frozenset(
    {"joint_pos", "joint_vel", "joint_effort", "object_position", "last_action", "actions"}
)
SUPPORTED_CRITIC_OBSERVATIONS = frozenset(
    {"joint_pos", "joint_vel", "joint_effort", "object_position", "object_velocity", "last_action", "actions"}
)
ACCEPTED_RECORD_STATUSES = frozenset({"validated", "released", "pass", "passed", "approved", "authored", "accepted"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _number(value: Any, label: str, *, positive: bool = False, non_negative: bool = False) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)),
        f"{label} must be finite",
    )
    number = float(value)
    if positive:
        _require(number > 0, f"{label} must be greater than zero")
    if non_negative:
        _require(number >= 0, f"{label} must be non-negative")
    return number


def _interval(value: Any, label: str, *, non_negative: bool = False) -> tuple[float, float]:
    _require(isinstance(value, list) and len(value) == 2, f"{label} must be a two-value interval")
    low = _number(value[0], f"{label}[0]", non_negative=non_negative)
    high = _number(value[1], f"{label}[1]", non_negative=non_negative)
    _require(low <= high, f"{label} must be ordered")
    return low, high


def _canonical_python(value: Any) -> Any:
    """Return an order-stable Python value for generated source literals."""

    if isinstance(value, dict):
        return {key: _canonical_python(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_python(item) for item in value]
    return value


def _exact_pattern(name: str) -> str:
    """Return an anchored regex that resolves one literal Isaac asset name."""

    return f"^{re.escape(name)}$"


def _preflight(manifest: dict[str, Any], manifest_sha256: str) -> dict[str, Any]:
    block = ((manifest.get("extensions") or {}).get("rl") or {}) if isinstance(manifest, dict) else {}
    _require(isinstance(block, dict) and block, "manifest carries no extensions.rl contract")
    _require(
        environment_contract_sha256(block) == block.get("contract_sha256"), "environment contract SHA-256 is stale"
    )
    runtime = block.get("runtime") if isinstance(block.get("runtime"), dict) else {}
    _require(runtime.get("physics_backend") == "physx", "the renderer supports the PhysX backend only")
    _require(runtime.get("isaac_lab_version") == "2.3.1", "the renderer targets Isaac Lab 2.3.1 exactly")
    _number(runtime.get("sim_dt"), "runtime.sim_dt", positive=True)
    _require(
        isinstance(runtime.get("decimation"), int)
        and not isinstance(runtime.get("decimation"), bool)
        and runtime["decimation"] >= 1,
        "runtime.decimation must be a positive integer",
    )
    _number(runtime.get("episode_length_s"), "runtime.episode_length_s", positive=True)
    _require(
        isinstance(runtime.get("num_envs"), int)
        and not isinstance(runtime.get("num_envs"), bool)
        and runtime["num_envs"] >= 1,
        "runtime.num_envs must be a positive integer",
    )

    task = block.get("task") if isinstance(block.get("task"), dict) else {}
    _require(task.get("behaviour") == "pick", "the renderer implements the pick contract only")
    embodiment = task.get("embodiment") if isinstance(task.get("embodiment"), dict) else {}
    _require(embodiment.get("status") == "validated", "embodiment is not validated")
    actuated = [item for item in embodiment.get("actuated_joints") or [] if isinstance(item, dict)]
    _require(bool(actuated), "embodiment has no actuated joints")
    names = [str(item.get("name") or "") for item in actuated]
    _require(all(names) and len(names) == len(set(names)), "embodiment joint names must be unique and non-empty")
    gripper_joints = [str(item) for item in embodiment.get("gripper_joints") or []]
    _require(
        bool(gripper_joints) and set(gripper_joints) < set(names), "embodiment needs explicit gripper and arm joints"
    )
    open_positions = (
        embodiment.get("gripper_open_positions") if isinstance(embodiment.get("gripper_open_positions"), dict) else {}
    )
    closed_positions = (
        embodiment.get("gripper_closed_positions")
        if isinstance(embodiment.get("gripper_closed_positions"), dict)
        else {}
    )
    _require(
        set(open_positions) == set(gripper_joints) and set(closed_positions) == set(gripper_joints),
        "embodiment must bind open and closed positions for every gripper joint",
    )
    _require(open_positions != closed_positions, "gripper open and closed position maps must differ")
    _require(
        str(embodiment.get("end_effector_body") or "") in set(embodiment.get("link_names") or []),
        "end-effector body is not an embodiment link",
    )
    _number(embodiment.get("action_scale"), "embodiment.action_scale", positive=True)
    maximum_aperture = _number(
        embodiment.get("maximum_gripper_aperture_m"),
        "embodiment.maximum_gripper_aperture_m",
        positive=True,
    )
    actuator = embodiment.get("actuator") if isinstance(embodiment.get("actuator"), dict) else {}
    _number(actuator.get("stiffness"), "embodiment.actuator.stiffness", non_negative=True)
    _number(actuator.get("damping"), "embodiment.actuator.damping", non_negative=True)
    _require(embodiment.get("format") == "urdf", "embodiment must be a URDF")
    _require(
        bool(embodiment.get("project_path")) and bool(embodiment.get("sha256")),
        "embodiment has no hashed project-local URDF",
    )
    _require(embodiment.get("fixed_base") is True, "the pick renderer requires a fixed-base embodiment")
    for item in actuated:
        joint_name = str(item.get("name") or "")
        _number(item.get("lower"), f"joint {joint_name} lower limit")
        _number(item.get("upper"), f"joint {joint_name} upper limit")
        _require(float(item["lower"]) <= float(item["upper"]), f"joint {joint_name} limits are reversed")
        _number(item.get("effort"), f"joint {joint_name} effort limit", positive=True)
        _number(item.get("velocity"), f"joint {joint_name} velocity limit", positive=True)
        if joint_name in gripper_joints:
            _require(item.get("type") == "prismatic", f"gripper joint {joint_name} must be prismatic")
            for label, positions in (("open", open_positions), ("closed", closed_positions)):
                value = _number(positions.get(joint_name), f"gripper joint {joint_name} {label} position")
                _require(
                    float(item["lower"]) <= value <= float(item["upper"]),
                    f"gripper joint {joint_name} {label} position lies outside its limits",
                )
        else:
            _require(
                item.get("type") in {"revolute", "continuous"},
                f"arm joint {joint_name} must use angular position units",
            )

    grasp_frames = task.get("grasp_frames") if isinstance(task.get("grasp_frames"), list) else []
    _require(bool(grasp_frames), "pick contract has no accepted grasp frames")
    grasp_ids: set[str] = set()
    for index, grasp in enumerate(grasp_frames):
        _require(isinstance(grasp, dict), f"task.grasp_frames[{index}] must be an object")
        grasp_id = str(grasp.get("id") or "")
        _require(bool(grasp_id) and grasp_id not in grasp_ids, "grasp frame IDs must be unique and non-empty")
        grasp_ids.add(grasp_id)
        _require(grasp.get("frame_space") == "asset", f"grasp frame {grasp_id} is not asset-local")
        frame = grasp.get("frame")
        _require(isinstance(frame, list) and len(frame) == 7, f"grasp frame {grasp_id} needs a seven-value pose")
        for component in frame:
            _number(component, f"grasp frame {grasp_id}")
        _require(grasp.get("quaternion_order") == "wxyz", f"grasp frame {grasp_id} must use wxyz quaternion order")
        quaternion_norm = math.sqrt(sum(float(component) ** 2 for component in frame[3:7]))
        _require(
            math.isclose(quaternion_norm, 1.0, rel_tol=0.0, abs_tol=1e-6),
            f"grasp frame {grasp_id} quaternion must be unit length",
        )
        approach = grasp.get("approach_vector")
        _require(
            isinstance(approach, list) and len(approach) == 3,
            f"grasp frame {grasp_id} needs a three-value approach vector",
        )
        for component in approach:
            _number(component, f"grasp frame {grasp_id} approach vector")
        approach_norm = math.sqrt(sum(float(component) ** 2 for component in approach))
        _require(
            math.isclose(approach_norm, 1.0, rel_tol=0.0, abs_tol=1e-6),
            f"grasp frame {grasp_id} approach vector must be unit length",
        )
        _number(grasp.get("gripper_width"), f"grasp frame {grasp_id} gripper width", positive=True)
        _require(
            float(grasp["gripper_width"]) <= maximum_aperture,
            f"grasp frame {grasp_id} exceeds the embodiment's maximum gripper aperture",
        )

    success = task.get("success") if isinstance(task.get("success"), dict) else {}
    _require(success.get("type") == "object_height_above_support", "pick success type is unsupported")
    _number(success.get("height_m"), "task.success.height_m", positive=True)
    _number(success.get("hold_seconds"), "task.success.hold_seconds", positive=True)

    scene = block.get("scene") if isinstance(block.get("scene"), dict) else {}
    _require(bool(scene.get("composed_root")), "scene has no composed asset USD")
    position = scene.get("object_initial_position")
    _require(
        isinstance(position, list) and len(position) == 3, "scene.object_initial_position must contain three values"
    )
    for component in position:
        _number(component, "scene.object_initial_position")
    _number(scene.get("support_height_m"), "scene.support_height_m")
    _require(
        math.isclose(float(scene["support_height_m"]), 0.0, rel_tol=0.0, abs_tol=1e-9),
        "the ground-supported pick renderer requires support_height_m zero",
    )
    _number(scene.get("env_spacing"), "scene.env_spacing", positive=True)
    _require(
        math.isclose(float(position[2]), float(scene["support_height_m"]), rel_tol=0.0, abs_tol=1e-9),
        "object initial root height must equal the support-surface height",
    )
    _require(scene.get("object_asset_type") == "rigid_object", "the pick renderer supports rigid objects only")

    _require(not block.get("sensor_contract"), "sensor contracts are not implemented by this renderer")
    observations = block.get("observations") if isinstance(block.get("observations"), dict) else {}
    policy_items = [item for item in observations.get("policy") or [] if isinstance(item, dict)]
    critic_items = [item for item in observations.get("critic") or [] if isinstance(item, dict)]
    policy_names = [str(item.get("term") or "") for item in policy_items]
    critic_names = [str(item.get("term") or "") for item in critic_items]
    _require(bool(policy_names), "policy observation group is empty")
    _require(len(policy_names) == len(set(policy_names)), "policy observations contain duplicate terms")
    _require(set(policy_names) <= SUPPORTED_POLICY_OBSERVATIONS, "policy observations contain an unsupported term")
    _require(len(critic_names) == len(set(critic_names)), "critic observations contain duplicate terms")
    _require(set(critic_names) <= SUPPORTED_CRITIC_OBSERVATIONS, "critic observations contain an unsupported term")
    policy_sources = {
        "joint_pos": "proprioception",
        "joint_vel": "proprioception",
        "joint_effort": "proprioception",
        "object_position": "simulator",
        "last_action": "control",
        "actions": "control",
    }
    for item in policy_items:
        term = str(item.get("term") or "")
        _require(item.get("source") == policy_sources[term], f"policy observation {term} has an unsupported source")
        _require(item.get("noise") is None, f"policy observation {term} requests unimplemented noise")
        _require(item.get("rate_hz") is None, f"policy observation {term} requests an unimplemented sample rate")
        if term in {"object_position", "last_action", "actions"}:
            _require(bool(item.get("manifest_path")), f"policy observation {term} has no evidence path")
            _require(
                item.get("record_status") in ACCEPTED_RECORD_STATUSES,
                f"policy observation {term} has no accepted evidence record",
            )
    for item in critic_items:
        term = str(item.get("term") or "")
        _require(bool(item.get("manifest_path")), f"critic observation {term} has no evidence path")
        _require(item.get("resolved") is True, f"critic observation {term} evidence is unresolved")
        _require(
            item.get("record_status") in ACCEPTED_RECORD_STATUSES,
            f"critic observation {term} has no accepted evidence record",
        )

    arm_joints = [name for name in names if name not in gripper_joints]
    expected_actions = [
        {
            "term": "arm_joint_position",
            "target": "embodiment",
            "joints": arm_joints,
            "mode": "position",
            "scale": embodiment.get("action_scale"),
            "limits_source": f"embodiment:{embodiment.get('source', '')}",
        },
        {
            "term": "gripper_joint_position",
            "target": "embodiment",
            "joints": [name for name in names if name in gripper_joints],
            "mode": "binary_position",
            "open_command": open_positions,
            "close_command": closed_positions,
            "limits_source": f"embodiment:{embodiment.get('source', '')}",
        },
    ]
    _require(
        block.get("actions") == expected_actions,
        "pick actions differ from the exact arm and gripper contract",
    )

    reward_items = [item for item in block.get("rewards") or [] if isinstance(item, dict)]
    _require(
        len(reward_items) == len(SUPPORTED_REWARDS)
        and {str(item.get("component") or "") for item in reward_items} == set(SUPPORTED_REWARDS),
        "pick rewards must contain reach, lift and action_rate exactly once",
    )
    for item in reward_items:
        component = str(item["component"])
        expected = SUPPORTED_REWARDS[component]
        _require(
            item.get("functional_form") == expected["functional_form"],
            f"reward {component} has an unsupported functional form",
        )
        _require(
            item.get("shaping_class") == expected["shaping_class"],
            f"reward {component} has an unsupported shaping class",
        )
        _number(item.get("weight"), f"reward {component} weight", non_negative=True)

    safety = block.get("safety") if isinstance(block.get("safety"), dict) else {}
    expected_terminations = [
        {"term": "time_out", "condition": "episode_length_s elapsed", "time_out": True},
        {
            "term": "success",
            "condition": str(task.get("success_definition") or ""),
            "type": success.get("type"),
            "height_m": success.get("height_m"),
            "hold_seconds": success.get("hold_seconds"),
            "time_out": False,
        },
        {
            "term": "illegal_contact",
            "condition": "contact force above the safety ceiling on the protected object",
            "force_ceiling_n": safety.get("contact_force_ceiling_n"),
            "time_out": False,
        },
        {
            "term": "joint_limit_violation",
            "condition": "embodiment joint outside its recorded limit",
            "time_out": False,
        },
    ]
    if safety.get("workspace_bounds"):
        expected_terminations.append(
            {"term": "workspace_exit", "condition": "end effector outside workspace_bounds", "time_out": False}
        )
    _require(block.get("terminations") == expected_terminations, "pick terminations differ from the task contract")

    reset_config = block.get("resets") if isinstance(block.get("resets"), list) else []
    reset_by_entity = {str(item.get("entity") or ""): item for item in reset_config if isinstance(item, dict)}
    _require(
        len(reset_by_entity) == len(reset_config) and set(reset_by_entity) == {"embodiment", "asset", "settle"},
        "pick resets must contain embodiment, asset and settle exactly once",
    )
    embodiment_reset = reset_by_entity["embodiment"]
    _require(
        embodiment_reset.get("distribution") == "uniform_offset_within_joint_limits"
        and embodiment_reset.get("feasibility_evidence") == "rl-probe-evidence:reset_feasibility",
        "embodiment reset semantics are unsupported",
    )
    embodiment_bounds = embodiment_reset.get("bounds") if isinstance(embodiment_reset.get("bounds"), dict) else {}
    _interval(embodiment_bounds.get("position_offset_rad"), "embodiment reset position_offset_rad")
    _require(embodiment_bounds.get("velocity") == [0.0, 0.0], "embodiment reset velocity must be zero")
    asset_reset = reset_by_entity["asset"]
    _require(
        asset_reset.get("distribution") == "pose_jitter_around_support"
        and asset_reset.get("feasibility_evidence") == "rl-probe-evidence:reset_feasibility",
        "asset reset semantics are unsupported",
    )
    asset_bounds = asset_reset.get("bounds") if isinstance(asset_reset.get("bounds"), dict) else {}
    _require(set(asset_bounds) == {"x_m", "y_m", "yaw_rad"}, "asset reset bounds are incomplete")
    for axis in ("x_m", "y_m", "yaw_rad"):
        _interval(asset_bounds.get(axis), f"asset reset {axis}")
    _require(
        asset_reset.get("seed_from_grasp_points") == [item["id"] for item in grasp_frames],
        "asset reset grasp seeds differ from the accepted grasp frames",
    )
    settle_reset = reset_by_entity["settle"]
    settle_parameters = runtime.get("settle_parameters") if isinstance(runtime.get("settle_parameters"), dict) else {}
    _require(
        settle_reset
        == {
            "entity": "settle",
            "distribution": "n/a",
            "bounds": {
                "settle_steps": settle_parameters.get("settle_steps"),
                "settled_speed_metres_per_second": settle_parameters.get("settled_speed_metres_per_second"),
            },
            "feasibility_evidence": "isaac-runtime-evidence:validation_parameters",
        },
        "settle reset differs from the validated runtime parameters",
    )
    _require(
        block.get("commands") == [{"term": "pick_grasp", "grasp_ids": [item["id"] for item in grasp_frames]}],
        "pick command contract differs from the accepted grasp frames",
    )

    randomisation = [item for item in block.get("randomisation") or [] if isinstance(item, dict)]
    randomisation_names = [str(item.get("axis") or "") for item in randomisation]
    _require(len(randomisation_names) == len(set(randomisation_names)), "randomisation contains duplicate axes")
    _require(set(randomisation_names) <= {"mass", "friction"}, "randomisation contains an unsupported axis")
    for item in randomisation:
        interval = _interval(item.get("interval"), f"randomisation {item.get('axis')} interval", non_negative=True)
        if item.get("axis") == "mass":
            _require(interval[0] > 0, "mass randomisation must remain strictly positive")
    _require(not block.get("variants"), "variant switching is not implemented by this renderer")
    curriculum = block.get("curriculum") if isinstance(block.get("curriculum"), dict) else {}
    _require(curriculum == {"tier": "none"}, "the renderer accepts curriculum tier none only")
    _require(
        manifest_sha256 == environment_manifest_sha256(manifest),
        "environment manifest SHA-256 does not match the stable source contract",
    )
    return block


def _observation_line(name: str, indent: str = "        ") -> str:
    mapping = {
        "joint_pos": "ObsTerm(func=mdp.joint_pos_rel)",
        "joint_vel": "ObsTerm(func=mdp.joint_vel_rel)",
        "joint_effort": 'ObsTerm(func=mdp.joint_effort, params={"asset_cfg": SceneEntityCfg("robot")})',
        "object_position": 'ObsTerm(func=custom_mdp.object_position_in_env, params={"asset_cfg": SceneEntityCfg("object")})',
        "object_velocity": 'ObsTerm(func=custom_mdp.object_velocity, params={"asset_cfg": SceneEntityCfg("object")})',
        "last_action": "ObsTerm(func=mdp.last_action)",
        "actions": "ObsTerm(func=mdp.last_action)",
    }
    return f"{indent}{name} = {mapping[name]}"


def render(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return source files and a render record after strict contract preflight."""

    manifest_sha256 = environment_manifest_sha256(manifest)
    block = _preflight(manifest, manifest_sha256)
    runtime = block["runtime"]
    task = block["task"]
    embodiment = task["embodiment"]
    scene = block["scene"]
    safety = block["safety"]
    actuated = embodiment["actuated_joints"]
    gripper_joints = list(embodiment["gripper_joints"])
    arm_joints = [str(item["name"]) for item in actuated if item["name"] not in gripper_joints]
    arm_joint_patterns = [_exact_pattern(name) for name in arm_joints]
    gripper_joint_patterns = [_exact_pattern(name) for name in gripper_joints]
    open_commands = {name: float(embodiment["gripper_open_positions"][name]) for name in gripper_joints}
    close_commands = {name: float(embodiment["gripper_closed_positions"][name]) for name in gripper_joints}
    open_command_patterns = {_exact_pattern(name): value for name, value in open_commands.items()}
    close_command_patterns = {_exact_pattern(name): value for name, value in close_commands.items()}
    policy_names = [str(item["term"]) for item in block["observations"]["policy"]]
    critic_names = [str(item["term"]) for item in block["observations"]["critic"]]
    rewards = {str(item["component"]): item for item in block["rewards"]}
    randomisation = {str(item["axis"]): item for item in block["randomisation"]}
    reset_map = {str(item["entity"]): item for item in block["resets"]}
    robot_offset = tuple(reset_map["embodiment"]["bounds"]["position_offset_rad"])
    pose = reset_map["asset"]["bounds"]
    success = task["success"]
    hold_steps = max(
        1,
        math.ceil(float(success["hold_seconds"]) / (float(runtime["sim_dt"]) * int(runtime["decimation"]))),
    )
    object_type = str(scene.get("object_asset_type") or "")
    _require(object_type == "rigid_object", "scene.object_asset_type is unsupported")
    actuator_lines = ["        actuators={"]
    for joint in actuated:
        joint_name = str(joint["name"])
        actuator_lines.extend(
            [
                f"            {joint_name!r}: ImplicitActuatorCfg(",
                f"                joint_names_expr={[_exact_pattern(joint_name)]!r},",
                f"                effort_limit_sim={float(joint['effort'])!r},",
                f"                velocity_limit_sim={float(joint['velocity'])!r},",
                f"                stiffness={float(embodiment['actuator']['stiffness'])!r},",
                f"                damping={float(embodiment['actuator']['damping'])!r},",
                "            ),",
            ]
        )
    actuator_lines.extend(["        },", "    )"])

    header = '''"""Isaac Lab environment generated from an asset-factory RL contract."""

from __future__ import annotations

from pathlib import Path

import isaaclab.envs.mdp as mdp
import isaaclab.sim as sim_utils
from isaaclab.actuators import ImplicitActuatorCfg
from isaaclab.assets import ArticulationCfg, AssetBaseCfg, RigidObjectCfg
from isaaclab.envs import ManagerBasedRLEnvCfg
from isaaclab.managers import EventTermCfg as EventTerm
from isaaclab.managers import ObservationGroupCfg as ObsGroup
from isaaclab.managers import ObservationTermCfg as ObsTerm
from isaaclab.managers import RewardTermCfg as RewTerm
from isaaclab.managers import SceneEntityCfg
from isaaclab.managers import TerminationTermCfg as DoneTerm
from isaaclab.scene import InteractiveSceneCfg
from isaaclab.sensors import ContactSensorCfg
from isaaclab.utils import configclass

from . import custom_mdp
'''
    lines = [
        header,
        f"MANIFEST_SHA256 = {manifest_sha256!r}",
        f"CONTRACT_SHA256 = {block['contract_sha256']!r}",
        f"PACKAGE_DEPENDENCY_FINGERPRINT = {runtime['validated_package_dependency_fingerprint']!r}",
        "PROJECT_DIR = Path(__file__).resolve().parents[1]",
        f"ASSET_USD_PATH = str(PROJECT_DIR / {scene['composed_root']!r})",
        f"ASSET_USD_SHA256 = {runtime['validated_usd_sha256']!r}",
        f"ROBOT_URDF_PATH = str(PROJECT_DIR / {embodiment['project_path']!r})",
        f"ROBOT_URDF_SHA256 = {embodiment['sha256']!r}",
        f"EE_BODY_NAME = {embodiment['end_effector_body']!r}",
        f"EE_BODY_PATTERN = {_exact_pattern(str(embodiment['end_effector_body']))!r}",
        f"GRASP_FRAMES = {_canonical_python(task['grasp_frames'])!r}",
        f"SUPPORT_HEIGHT_M = {float(scene['support_height_m'])!r}",
        f"SUCCESS_HEIGHT_M = {float(success['height_m'])!r}",
        f"SUCCESS_HOLD_STEPS = {hold_steps}",
        "",
        "@configclass",
        "class AssetFactorySceneCfg(InteractiveSceneCfg):",
        '    ground = AssetBaseCfg(prim_path="/World/ground", spawn=sim_utils.GroundPlaneCfg(), init_state=AssetBaseCfg.InitialStateCfg(pos=(0.0, 0.0, SUPPORT_HEIGHT_M)))',
        '    light = AssetBaseCfg(prim_path="/World/light", spawn=sim_utils.DomeLightCfg(intensity=3000.0))',
        "    robot = ArticulationCfg(",
        '        prim_path="{ENV_REGEX_NS}/Robot",',
        "        spawn=sim_utils.UrdfFileCfg(",
        "            asset_path=ROBOT_URDF_PATH,",
        f"            fix_base={embodiment['fixed_base']!r},",
        "            merge_fixed_joints=False,",
        "            collision_from_visuals=False,",
        "            force_usd_conversion=True,",
        "            make_instanceable=True,",
        "            activate_contact_sensors=True,",
        "        ),",
        f"        init_state=ArticulationCfg.InitialStateCfg(joint_pos={open_command_patterns!r}),",
        *actuator_lines,
    ]
    initial_position = tuple(float(value) for value in scene["object_initial_position"])
    lines.extend(
        [
            "    object = RigidObjectCfg(",
            '        prim_path="{ENV_REGEX_NS}/Object",',
            "        spawn=sim_utils.UsdFileCfg(usd_path=ASSET_USD_PATH, activate_contact_sensors=True),",
            f"        init_state=RigidObjectCfg.InitialStateCfg(pos={initial_position!r}),",
            "    )",
        ]
    )
    lines.extend(
        [
            "    contact_forces = ContactSensorCfg(",
            '        prim_path="{ENV_REGEX_NS}/Object",',
            "        update_period=0.0,",
            "        history_length=1,",
            "        track_air_time=False,",
            "        max_contact_data_count_per_prim=64,",
            '        filter_prim_paths_expr=["/World/ground/GroundPlane/CollisionPlane", "{ENV_REGEX_NS}/Robot/.*"],',
            "    )",
            "",
            "@configclass",
            "class ObservationsCfg:",
            "    @configclass",
            "    class PolicyCfg(ObsGroup):",
        ]
    )
    lines.extend(_observation_line(name) for name in policy_names)
    lines.extend(
        [
            "",
            "        def __post_init__(self) -> None:",
            "            self.enable_corruption = False",
            "            self.concatenate_terms = True",
        ]
    )
    if critic_names:
        lines.extend(["", "    @configclass", "    class CriticCfg(ObsGroup):"])
        lines.extend(_observation_line(name) for name in critic_names)
        lines.extend(
            [
                "",
                "        def __post_init__(self) -> None:",
                "            self.enable_corruption = False",
                "            self.concatenate_terms = True",
                "",
                "    policy: PolicyCfg = PolicyCfg()",
                "    critic: CriticCfg = CriticCfg()",
            ]
        )
    else:
        lines.extend(["", "    policy: PolicyCfg = PolicyCfg()"])
    lines.extend(
        [
            "",
            "@configclass",
            "class ActionsCfg:",
            f'    arm = mdp.JointPositionActionCfg(asset_name="robot", joint_names={arm_joint_patterns!r}, scale={float(embodiment["action_scale"])!r}, use_default_offset=True, preserve_order=True)',
            f'    gripper = mdp.BinaryJointPositionActionCfg(asset_name="robot", joint_names={gripper_joint_patterns!r}, open_command_expr={open_command_patterns!r}, close_command_expr={close_command_patterns!r})',
            "",
            "@configclass",
            "class RewardsCfg:",
            f'    reach = RewTerm(func=custom_mdp.reach_reward, weight={float(rewards["reach"]["weight"])!r}, params={{"grasp_frames": GRASP_FRAMES, "asset_cfg": SceneEntityCfg("object"), "robot_cfg": SceneEntityCfg("robot"), "ee_body_pattern": EE_BODY_PATTERN, "ee_body_name": EE_BODY_NAME}})',
            f'    lift = RewTerm(func=custom_mdp.lift_progress, weight={float(rewards["lift"]["weight"])!r}, params={{"support_height_m": SUPPORT_HEIGHT_M, "asset_cfg": SceneEntityCfg("object")}})',
            f"    action_rate = RewTerm(func=custom_mdp.action_rate_penalty, weight={float(rewards['action_rate']['weight'])!r})",
            "",
            "@configclass",
            "class TerminationsCfg:",
            "    time_out = DoneTerm(func=mdp.time_out, time_out=True)",
            '    success = DoneTerm(func=custom_mdp.lift_held, params={"support_height_m": SUPPORT_HEIGHT_M, "height_m": SUCCESS_HEIGHT_M, "hold_steps": SUCCESS_HOLD_STEPS, "asset_cfg": SceneEntityCfg("object")})',
            f'    illegal_contact = DoneTerm(func=custom_mdp.illegal_contact, params={{"sensor_name": "contact_forces", "force_ceiling_n": {float(safety["contact_force_ceiling_n"])!r}}})',
            '    joint_limit_violation = DoneTerm(func=custom_mdp.joint_limit_violation, params={"asset_cfg": SceneEntityCfg("robot")})',
        ]
    )
    if safety.get("workspace_bounds"):
        lines.append(
            f'    workspace_exit = DoneTerm(func=custom_mdp.workspace_exit, params={{"bounds": {_canonical_python(safety["workspace_bounds"])!r}, "robot_cfg": SceneEntityCfg("robot"), "ee_body_pattern": EE_BODY_PATTERN, "ee_body_name": EE_BODY_NAME}})'
        )
    lines.extend(["", "@configclass", "class EventCfg:"])
    if "mass" in randomisation:
        low, high = (float(value) for value in randomisation["mass"]["interval"])
        lines.append(
            f'    randomise_mass = EventTerm(func=mdp.randomize_rigid_body_mass, mode="reset", params={{"asset_cfg": SceneEntityCfg("object"), "mass_distribution_params": ({low!r}, {high!r}), "operation": "abs", "distribution": "uniform"}})'
        )
    if "friction" in randomisation:
        low, high = (float(value) for value in randomisation["friction"]["interval"])
        lines.append(
            f'    randomise_friction = EventTerm(func=mdp.randomize_rigid_body_material, mode="reset", params={{"asset_cfg": SceneEntityCfg("object"), "static_friction_range": ({low!r}, {high!r}), "dynamic_friction_range": ({low!r}, {high!r}), "restitution_range": (0.0, 0.0), "num_buckets": 64, "make_consistent": True}})'
        )
    lines.extend(
        [
            f'    reset_object = EventTerm(func=mdp.reset_root_state_uniform, mode="reset", params={{"pose_range": {{"x": {tuple(pose["x_m"])!r}, "y": {tuple(pose["y_m"])!r}, "yaw": {tuple(pose["yaw_rad"])!r}}}, "velocity_range": {{}}, "asset_cfg": SceneEntityCfg("object")}})',
            f'    reset_arm = EventTerm(func=mdp.reset_joints_by_offset, mode="reset", params={{"position_range": {robot_offset!r}, "velocity_range": (0.0, 0.0), "asset_cfg": SceneEntityCfg("robot", joint_names={arm_joint_patterns!r}, preserve_order=True)}})',
            f'    reset_gripper = EventTerm(func=mdp.reset_joints_by_offset, mode="reset", params={{"position_range": (0.0, 0.0), "velocity_range": (0.0, 0.0), "asset_cfg": SceneEntityCfg("robot", joint_names={gripper_joint_patterns!r}, preserve_order=True)}})',
            '    reset_task_state = EventTerm(func=custom_mdp.reset_task_state, mode="reset")',
            "",
            "@configclass",
            "class AssetFactoryEnvCfg(ManagerBasedRLEnvCfg):",
            f"    scene: AssetFactorySceneCfg = AssetFactorySceneCfg(num_envs={int(runtime['num_envs'])}, env_spacing={float(scene['env_spacing'])!r})",
            "    observations: ObservationsCfg = ObservationsCfg()",
            "    actions: ActionsCfg = ActionsCfg()",
            "    rewards: RewardsCfg = RewardsCfg()",
            "    terminations: TerminationsCfg = TerminationsCfg()",
            "    events: EventCfg = EventCfg()",
            "",
            "    def __post_init__(self) -> None:",
            f"        self.decimation = {int(runtime['decimation'])}",
            f"        self.episode_length_s = {float(runtime['episode_length_s'])!r}",
            f"        self.sim.dt = {float(runtime['sim_dt'])!r}",
            f"        self.sim.render_interval = {int(runtime['render_interval'])}",
        ]
    )
    env_source = "\n".join(lines) + "\n"

    custom_source = '''"""MDP terms for the generated PhysX pick environment."""

from __future__ import annotations

import torch
from isaaclab.managers import SceneEntityCfg
from isaaclab.utils.math import quat_apply


def _ee_position(env, robot_cfg: SceneEntityCfg, ee_body_pattern: str, ee_body_name: str) -> torch.Tensor:
    robot = env.scene[robot_cfg.name]
    body_ids, body_names = robot.find_bodies(ee_body_pattern)
    if len(body_ids) != 1:
        raise RuntimeError(f"end-effector pattern {ee_body_pattern!r} resolved to {len(body_ids)} bodies")
    if body_names[0] != ee_body_name:
        raise RuntimeError(f"end-effector pattern {ee_body_pattern!r} did not resolve literally")
    return robot.data.body_pos_w[:, body_ids[0]]


def _grasp_positions(env, grasp_frames: list[dict], asset_cfg: SceneEntityCfg) -> torch.Tensor:
    asset = env.scene[asset_cfg.name]
    offsets = torch.tensor([item["frame"][:3] for item in grasp_frames], dtype=torch.float32, device=env.device)
    count = offsets.shape[0]
    quaternions = asset.data.root_quat_w[:, None, :].expand(-1, count, -1).reshape(-1, 4)
    local = offsets[None, :, :].expand(env.num_envs, -1, -1).reshape(-1, 3)
    rotated = quat_apply(quaternions, local).reshape(env.num_envs, count, 3)
    return asset.data.root_pos_w[:, None, :] + rotated


def object_position_in_env(env, asset_cfg: SceneEntityCfg) -> torch.Tensor:
    return env.scene[asset_cfg.name].data.root_pos_w - env.scene.env_origins


def object_velocity(env, asset_cfg: SceneEntityCfg) -> torch.Tensor:
    return env.scene[asset_cfg.name].data.root_lin_vel_w


def reach_reward(env, grasp_frames: list[dict], asset_cfg: SceneEntityCfg, robot_cfg: SceneEntityCfg, ee_body_pattern: str, ee_body_name: str) -> torch.Tensor:
    targets = _grasp_positions(env, grasp_frames, asset_cfg)
    distance = torch.linalg.vector_norm(targets - _ee_position(env, robot_cfg, ee_body_pattern, ee_body_name)[:, None, :], dim=-1).amin(dim=1)
    return 1.0 - torch.tanh(distance / 0.1)


def lift_progress(env, support_height_m: float, asset_cfg: SceneEntityCfg) -> torch.Tensor:
    height = env.scene[asset_cfg.name].data.root_pos_w[:, 2] - support_height_m
    return torch.clamp(height, min=0.0, max=0.1) / 0.1


def action_rate_penalty(env) -> torch.Tensor:
    delta = env.action_manager.action - env.action_manager.prev_action
    return -torch.sum(torch.square(delta), dim=1)


def reset_task_state(env, env_ids) -> None:
    counter = getattr(env, "_afb_lift_hold_counter", None)
    if counter is None or counter.shape[0] != env.num_envs:
        counter = torch.zeros(env.num_envs, dtype=torch.long, device=env.device)
    if env_ids is None:
        counter.zero_()
    else:
        counter[env_ids] = 0
    env._afb_lift_hold_counter = counter


def lift_held(env, support_height_m: float, height_m: float, hold_steps: int, asset_cfg: SceneEntityCfg) -> torch.Tensor:
    lifted = env.scene[asset_cfg.name].data.root_pos_w[:, 2] >= support_height_m + height_m
    counter = getattr(env, "_afb_lift_hold_counter", None)
    if counter is None or counter.shape[0] != env.num_envs:
        counter = torch.zeros(env.num_envs, dtype=torch.long, device=env.device)
    counter = torch.where(lifted, counter + 1, torch.zeros_like(counter))
    env._afb_lift_hold_counter = counter
    result = counter >= hold_steps
    env._afb_step_success = result.clone()
    return result


def illegal_contact(env, sensor_name: str, force_ceiling_n: float) -> torch.Tensor:
    forces = env.scene.sensors[sensor_name].data.net_forces_w
    return torch.linalg.vector_norm(forces, dim=-1).amax(dim=1) > force_ceiling_n


def joint_limit_violation(env, asset_cfg: SceneEntityCfg) -> torch.Tensor:
    asset = env.scene[asset_cfg.name]
    limits = asset.data.soft_joint_pos_limits
    result = ((asset.data.joint_pos < limits[..., 0]) | (asset.data.joint_pos > limits[..., 1])).any(dim=1)
    env._afb_step_joint_limit_violation = result.clone()
    return result


def workspace_exit(env, bounds: dict, robot_cfg: SceneEntityCfg, ee_body_pattern: str, ee_body_name: str) -> torch.Tensor:
    position = _ee_position(env, robot_cfg, ee_body_pattern, ee_body_name) - env.scene.env_origins
    outside = torch.zeros(env.num_envs, dtype=torch.bool, device=env.device)
    for axis, key in enumerate(("x", "y", "z")):
        if key in bounds:
            low, high = bounds[key]
            outside |= (position[:, axis] < low) | (position[:, axis] > high)
    return outside
'''
    init_source = (
        '"""Generated asset-factory Isaac Lab environment."""\n\n'
        'from .rl_env_cfg import AssetFactoryEnvCfg\n\n__all__ = ["AssetFactoryEnvCfg"]\n'
    )
    record = {
        "renderer": {"id": RENDERER_ID, "version": RENDERER_VERSION},
        "manifest_sha256": manifest_sha256,
        "contract_sha256": block["contract_sha256"],
        "package_dependency_fingerprint": runtime["validated_package_dependency_fingerprint"],
        "physics_backend": "physx",
        "asset_usd": scene["composed_root"],
        "asset_usd_sha256": runtime["validated_usd_sha256"],
        "robot_urdf": embodiment["project_path"],
        "robot_urdf_sha256": embodiment["sha256"],
        "ready": True,
    }
    return {
        "__init__.py": init_source,
        "rl_env_cfg.py": env_source,
        "custom_mdp.py": custom_source,
        "record": record,
    }


def _confined_file(project: Path, value: str, label: str) -> Path:
    raw = Path(value)
    _require(bool(value) and not raw.is_absolute() and ".." not in raw.parts, f"{label} must be project-relative")
    candidate = project / raw
    cursor = project
    for part in raw.parts:
        cursor /= part
        _require(not cursor.is_symlink(), f"{label} must not traverse a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is missing or outside the project") from exc
    _require(resolved.is_file(), f"{label} must identify a regular file")
    return resolved


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
    except BaseException:
        durable_unlink(temporary, missing_ok=True)
        raise


def verify_rendered_package_sources(project: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Verify generated Python and its record byte-for-byte against the current contract."""

    project = project.resolve(strict=True)
    expected = render(manifest)
    source_names = ("__init__.py", "rl_env_cfg.py", "custom_mdp.py")
    hashes: dict[str, str] = {}
    for name in source_names:
        path = _confined_file(project, f"envs/{name}", f"rendered {name}")
        actual = path.read_text(encoding="utf-8")
        _require(actual == expected[name], f"rendered {name} differs from the current environment contract")
        hashes[name] = sha256_file(path)
    record_path = _confined_file(project, "envs/rl_env_cfg.render.json", "render record")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    expected_record = {
        **expected["record"],
        "files": {name: hashlib.sha256(str(expected[name]).encode("utf-8")).hexdigest() for name in source_names},
    }
    _require(record == expected_record, "render record differs from the current environment contract")
    hashes["rl_env_cfg.render.json"] = sha256_file(record_path)
    return hashes


def _render_to_directory_locked(manifest_path: Path, output_dir: Path, project: Path) -> dict[str, Any]:
    manifest_path.relative_to(project)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    block = (manifest.get("extensions") or {}).get("rl") or {}
    scene = block.get("scene") or {}
    embodiment = (block.get("task") or {}).get("embodiment") or {}
    asset_path = _confined_file(project, str(scene.get("composed_root") or ""), "asset USD")
    robot_path = _confined_file(project, str(embodiment.get("project_path") or ""), "robot URDF")
    _require(
        sha256_file(asset_path) == str((block.get("runtime") or {}).get("validated_usd_sha256") or ""),
        "asset USD SHA-256 differs from the runtime binding",
    )
    _require(
        sha256_file(robot_path) == str(embodiment.get("sha256") or ""),
        "robot URDF SHA-256 differs from the embodiment binding",
    )
    package = package_inventory_fingerprint(asset_path.parent)
    _require(package["status"] == "pass", "asset package inventory cannot be verified")
    _require(
        package["fingerprint"]
        == str((block.get("runtime") or {}).get("validated_package_dependency_fingerprint") or ""),
        "asset package fingerprint differs from the runtime binding",
    )

    result = render(manifest)
    output = output_dir.resolve(strict=False)
    expected_output = (project / "envs").resolve(strict=False)
    _require(output == expected_output, "render output directory must be the project's envs directory")
    cursor = project
    for part in output.relative_to(project).parts:
        cursor /= part
        _require(not cursor.is_symlink(), "render output directory must not traverse a symbolic link")
    output.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "rl_env_cfg.py", "custom_mdp.py"):
        _atomic_write(output / name, result[name])
    record = dict(result["record"])
    record["files"] = {name: sha256_file(output / name) for name in ("__init__.py", "rl_env_cfg.py", "custom_mdp.py")}
    _atomic_write(output / "rl_env_cfg.render.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def render_to_directory(manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    """Render into the project while holding its mutation lease."""

    resolved_manifest = manifest_path.resolve(strict=True)
    project = resolved_manifest.parent.parent.resolve(strict=True)
    with workspace_lease(project, "rl-environment-render"):
        return _render_to_directory_locked(resolved_manifest, output_dir, project)
