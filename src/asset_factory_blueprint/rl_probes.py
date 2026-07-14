"""Runtime-independent acceptance logic for the RL environment probes."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from asset_factory_blueprint.rl_evidence import (
    PROBE_PROTOCOL_ID,
    PROBE_PROTOCOL_VERSION,
    PROBE_REPORT_ID,
    PROBE_REPORT_VERSION,
)

GAMING_PATTERNS = (
    "proximity_without_contact",
    "velocity_without_displacement",
    "oscillation",
    "early_termination",
)


@dataclass
class StepResult:
    """One environment step across all parallel environments."""

    rewards: list[float]
    components: dict[str, list[float]] = field(default_factory=dict)
    terminated: list[bool] = field(default_factory=list)
    truncated: list[bool] = field(default_factory=list)
    success: list[bool] = field(default_factory=list)


@dataclass
class RolloutResult:
    returns: list[float]
    components: dict[str, list[float]]
    problems: list[str]
    terminal_steps: list[int | None]
    terminal_success: list[bool]


class ProbeEnvironment(Protocol):
    """Runtime operations required by the acceptance probes."""

    num_envs: int
    component_names: list[str]
    grasp_ids: list[str]

    def reset(self, seed: int) -> None: ...

    def step(self, actions: list[list[float]]) -> StepResult: ...

    def zero_actions(self) -> list[list[float]]: ...

    def random_actions(self, rng: random.Random) -> list[list[float]]: ...

    def select_grasp(self, grasp_id: str) -> None: ...

    def oracle_actions(self, phase: str, step: int) -> list[list[float]]: ...

    def gaming_actions(self, pattern: str, step: int) -> list[list[float]]: ...

    def object_positions(self) -> list[list[float]]: ...

    def object_speeds(self) -> list[float]: ...

    def penetration_depths(self) -> list[float]: ...

    def joint_limit_violations(self) -> list[int]: ...

    def grasp_frame_reached(self) -> list[bool]: ...

    def task_success(self) -> list[bool]: ...


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _finite_all(values: list[float]) -> bool:
    return all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values
    )


def _distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def _normalise_step(result: StepResult, num_envs: int, step: int) -> list[str]:
    problems: list[str] = []
    for name, values in (
        ("rewards", result.rewards),
        ("terminated", result.terminated),
        ("truncated", result.truncated),
        ("success", result.success),
    ):
        if len(values) != num_envs:
            problems.append(f"step {step} returned {len(values)} {name} values for {num_envs} environments")
    if not _finite_all(result.rewards):
        problems.append(f"non-finite reward at step {step}")
    for name, values in result.components.items():
        if len(values) != num_envs:
            problems.append(f"reward component {name} has {len(values)} values for {num_envs} environments")
        elif not _finite_all(values):
            problems.append(f"non-finite reward component {name} at step {step}")
    return problems


def _rollout(env: ProbeEnvironment, seed: int, steps: int, policy) -> RolloutResult:
    """Run a policy while retaining each environment's first terminal transition."""

    env.reset(seed)
    returns = [0.0] * env.num_envs
    totals: dict[str, list[float]] = {}
    active = [True] * env.num_envs
    terminal_steps: list[int | None] = [None] * env.num_envs
    terminal_success = [False] * env.num_envs
    problems: list[str] = []
    for step in range(steps):
        result = env.step(policy(step))
        step_problems = _normalise_step(result, env.num_envs, step)
        if step_problems:
            problems.extend(step_problems)
            break
        for index, value in enumerate(result.rewards):
            if active[index]:
                returns[index] += float(value)
        for name, values in result.components.items():
            bucket = totals.setdefault(name, [0.0] * env.num_envs)
            for index, value in enumerate(values):
                if active[index]:
                    bucket[index] += float(value)
        for index, is_active in enumerate(active):
            if not is_active:
                continue
            if result.terminated[index] or result.truncated[index]:
                terminal_steps[index] = step
                terminal_success[index] = bool(result.success[index])
                active[index] = False
        if not any(active):
            break
    return RolloutResult(returns, totals, problems, terminal_steps, terminal_success)


def run_smoke(env: ProbeEnvironment, seed: int, steps: int) -> dict[str, Any]:
    zero = _rollout(env, seed, steps, lambda _step: env.zero_actions())
    rng = random.Random(seed)
    random_result = _rollout(env, seed, steps, lambda _step: env.random_actions(rng))
    problems = zero.problems + random_result.problems
    random_mean = _mean(random_result.returns)
    metrics = {
        "steps": steps,
        "seed": seed,
        "zero_action_return_mean": _mean(zero.returns),
        "random_policy_return_mean": random_mean,
        "random_policy_return_std": math.sqrt(_mean([(value - random_mean) ** 2 for value in random_result.returns])),
        "random_policy_component_means": {name: _mean(values) for name, values in random_result.components.items()},
        "zero_terminal_steps": zero.terminal_steps,
        "random_terminal_steps": random_result.terminal_steps,
    }
    return {
        "probe": "smoke",
        "status": "pass" if not problems else "blocked",
        "reason": "; ".join(problems),
        "metrics": metrics,
    }


def _oracle_grasp(
    env: ProbeEnvironment,
    seed: int,
    grasp_id: str,
    phase_steps: dict[str, int],
) -> tuple[dict[str, Any], dict[str, list[float]], list[str]]:
    env.select_grasp(grasp_id)
    env.reset(seed)
    returns = [0.0] * env.num_envs
    totals: dict[str, list[float]] = {}
    active = [True] * env.num_envs
    terminal_success = [False] * env.num_envs
    terminal_steps: list[int | None] = [None] * env.num_envs
    reached = [False] * env.num_envs
    problems: list[str] = []
    global_step = 0
    violations = 0
    for phase in ("approach", "descend", "close", "lift"):
        if not any(active):
            break
        steps = phase_steps.get(phase)
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
            problems.append(f"oracle phase {phase} has no positive step count")
            continue
        for local_step in range(steps):
            result = env.step(env.oracle_actions(phase, local_step))
            step_problems = _normalise_step(result, env.num_envs, global_step)
            if step_problems:
                problems.extend(step_problems)
                break
            for index, value in enumerate(result.rewards):
                if active[index]:
                    returns[index] += float(value)
            for name, values in result.components.items():
                bucket = totals.setdefault(name, [0.0] * env.num_envs)
                for index, value in enumerate(values):
                    if active[index]:
                        bucket[index] += float(value)
            for index, is_active in enumerate(active):
                if is_active and (result.terminated[index] or result.truncated[index]):
                    terminal_steps[index] = global_step
                    terminal_success[index] = bool(result.success[index])
                    active[index] = False
            global_step += 1
            if not any(active):
                break
        violations += sum(env.joint_limit_violations())
        if phase == "descend":
            reached = [value and active[index] for index, value in enumerate(env.grasp_frame_reached())]
    live_success = env.task_success()
    success = [terminal_success[index] or (active[index] and live_success[index]) for index in range(env.num_envs)]
    return (
        {
            "grasp_id": grasp_id,
            "reachable_fraction": _mean([1.0 if value else 0.0 for value in reached]),
            "success_rate": _mean([1.0 if value else 0.0 for value in success]),
            "return_mean": _mean(returns),
            "returns": list(returns),
            "limit_violations": violations,
            "terminal_steps": terminal_steps,
        },
        totals,
        problems,
    )


def run_oracle(env: ProbeEnvironment, seed: int, phase_steps: dict[str, int]) -> dict[str, Any]:
    problems: list[str] = []
    per_grasp: list[dict[str, Any]] = []
    component_values: dict[str, list[float]] = {}
    if not env.grasp_ids:
        problems.append("environment exposes no accepted grasp IDs")
    for grasp_id in env.grasp_ids:
        result, totals, grasp_problems = _oracle_grasp(env, seed, grasp_id, phase_steps)
        per_grasp.append(result)
        problems.extend(grasp_problems)
        for name, values in totals.items():
            component_values.setdefault(name, []).extend(values)
        if result["reachable_fraction"] != 1.0:
            problems.append(f"grasp {grasp_id} is not reachable in every environment")
        if result["success_rate"] != 1.0:
            problems.append(f"grasp {grasp_id} does not complete the task in every environment")
        if result["limit_violations"]:
            problems.append(f"grasp {grasp_id} caused {result['limit_violations']} joint limit violations")
    reachable = _mean([float(item["reachable_fraction"]) for item in per_grasp])
    success_rate = _mean([float(item["success_rate"]) for item in per_grasp])
    oracle_returns = (
        [max(float(item["returns"][env_index]) for item in per_grasp) for env_index in range(env.num_envs)]
        if per_grasp
        else []
    )
    metrics = {
        "seed": seed,
        "phase_steps": dict(phase_steps),
        "oracle_return_mean": _mean([float(item["return_mean"]) for item in per_grasp]),
        "oracle_returns": oracle_returns,
        "oracle_component_means": {name: _mean(values) for name, values in component_values.items()},
        "oracle_success_rate": success_rate,
        "reachable_grasp_fraction": reachable,
        "limit_violations": sum(int(item["limit_violations"]) for item in per_grasp),
        "per_grasp": per_grasp,
        "component_dominance_status": "pending",
        "component_dominance_reason": "",
    }
    return {
        "probe": "oracle",
        "status": "pass" if not problems else "blocked",
        "reason": "; ".join(problems),
        "metrics": metrics,
    }


def run_reset_feasibility(
    env: ProbeEnvironment,
    seeds: list[int],
    settle_steps: int,
    settled_speed: float,
    penetration_tolerance_m: float,
) -> dict[str, Any]:
    infeasible = 0
    total = 0
    worst_penetration = 0.0
    worst_speed = 0.0
    for seed in seeds:
        env.reset(seed)
        terminated_during_settle = [False] * env.num_envs
        for step in range(settle_steps):
            result = env.step(env.zero_actions())
            problems = _normalise_step(result, env.num_envs, step)
            if problems:
                raise RuntimeError("; ".join(problems))
            for index in range(env.num_envs):
                terminated_during_settle[index] |= bool(result.terminated[index] or result.truncated[index])
        depths = env.penetration_depths()
        speeds = env.object_speeds()
        if len(depths) != env.num_envs or len(speeds) != env.num_envs:
            raise RuntimeError("reset measurements do not cover every environment")
        for index, (depth, speed) in enumerate(zip(depths, speeds, strict=True)):
            if not _finite_all([depth, speed]):
                raise RuntimeError("reset measurements contain a non-finite value")
            total += 1
            worst_penetration = max(worst_penetration, depth)
            worst_speed = max(worst_speed, speed)
            if terminated_during_settle[index] or depth > penetration_tolerance_m or speed > settled_speed:
                infeasible += 1
    fraction = infeasible / total if total else 1.0
    problems: list[str] = []
    if total == 0:
        problems.append("no resets were sampled")
    if infeasible:
        problems.append(
            f"{infeasible} of {total} resets did not settle; worst penetration {worst_penetration:.4f} m and worst speed {worst_speed:.3f} m/s"
        )
    metrics = {
        "seeds": list(seeds),
        "settle_steps": settle_steps,
        "settled_speed_metres_per_second": settled_speed,
        "penetration_tolerance_m": penetration_tolerance_m,
        "penetration_measured": True,
        "infeasible_reset_fraction": fraction,
        "worst_penetration_m": worst_penetration,
        "worst_settled_speed": worst_speed,
    }
    return {
        "probe": "reset",
        "status": "pass" if not problems else "blocked",
        "reason": "; ".join(problems),
        "metrics": metrics,
    }


def run_repeatability(env: ProbeEnvironment, seed: int, steps: int, tolerance_m: float) -> dict[str, Any]:
    def trajectory() -> tuple[list[list[list[float]]], list[float], list[tuple[list[bool], list[bool], list[bool]]]]:
        rng = random.Random(seed)
        env.reset(seed)
        positions: list[list[list[float]]] = []
        returns = [0.0] * env.num_envs
        active = [True] * env.num_envs
        transitions: list[tuple[list[bool], list[bool], list[bool]]] = []
        for step in range(steps):
            result = env.step(env.random_actions(rng))
            problems = _normalise_step(result, env.num_envs, step)
            if problems:
                raise RuntimeError("; ".join(problems))
            for index, value in enumerate(result.rewards):
                if active[index]:
                    returns[index] += float(value)
            step_positions = env.object_positions()
            if len(step_positions) != env.num_envs:
                raise RuntimeError("object positions do not cover every environment")
            for position in step_positions:
                if len(position) != 3 or not _finite_all(position):
                    raise RuntimeError("object positions must contain three finite coordinates")
            positions.append(step_positions)
            transitions.append((list(result.terminated), list(result.truncated), list(result.success)))
            for index in range(env.num_envs):
                if active[index] and (result.terminated[index] or result.truncated[index]):
                    active[index] = False
            if not any(active):
                break
        return positions, returns, transitions

    first_positions, first_returns, first_transitions = trajectory()
    second_positions, second_returns, second_transitions = trajectory()
    if len(first_positions) != len(second_positions):
        raise RuntimeError("seeded rollouts terminated at different steps")
    if first_transitions != second_transitions:
        raise RuntimeError("seeded rollouts produced different terminal transitions")
    trajectory_deviation = 0.0
    for first_step, second_step in zip(first_positions, second_positions, strict=True):
        for first, second in zip(first_step, second_step, strict=True):
            trajectory_deviation = max(trajectory_deviation, _distance(first, second))
    return_deviation = max(
        (abs(first - second) for first, second in zip(first_returns, second_returns, strict=True)),
        default=0.0,
    )
    passed = trajectory_deviation <= tolerance_m and return_deviation <= 1e-6
    reason = ""
    if not passed:
        reason = (
            f"seeded rollouts differ by {trajectory_deviation:.4f} m and {return_deviation:.6g} return; "
            f"limits are {tolerance_m} m and 1e-6 return"
        )
    metrics = {
        "seed": seed,
        "steps": steps,
        "tolerance_m": tolerance_m,
        "return_tolerance": 1e-6,
        "max_trajectory_deviation_m": trajectory_deviation,
        "max_return_deviation": return_deviation,
    }
    return {"probe": "repeat", "status": "pass" if passed else "blocked", "reason": reason, "metrics": metrics}


def run_gaming_probes(
    env: ProbeEnvironment,
    seed: int,
    steps: int,
    oracle_returns: list[float],
    allowed_fraction: float,
    patterns: tuple[str, ...] = GAMING_PATTERNS,
) -> dict[str, Any]:
    problems: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    if tuple(patterns) != GAMING_PATTERNS:
        problems.append("gaming probe must run the complete canonical pattern set")
    baseline_valid = (
        len(oracle_returns) == env.num_envs
        and _finite_all(oracle_returns)
        and all(value > 0 for value in oracle_returns)
    )
    if not baseline_valid:
        problems.append("oracle return baseline must contain one finite, strictly positive value per environment")
    baselines = oracle_returns if baseline_valid else [0.0] * env.num_envs
    for pattern in patterns:
        if pattern not in GAMING_PATTERNS:
            problems.append(f"unsupported gaming pattern {pattern}")
            continue
        rollout = _rollout(
            env,
            seed,
            steps,
            lambda step, selected=pattern: env.gaming_actions(selected, step),
        )
        earned = _mean(rollout.returns)
        fractions = (
            [value / oracle for value, oracle in zip(rollout.returns, baselines, strict=True)]
            if baseline_valid
            else [0.0] * env.num_envs
        )
        fraction = _mean(fractions)
        live_success = env.task_success()
        succeeded = [
            rollout.terminal_success[index] or (rollout.terminal_steps[index] is None and bool(live_success[index]))
            for index in range(env.num_envs)
        ]
        gamed_environments = [
            index
            for index, (share, success) in enumerate(zip(fractions, succeeded, strict=True))
            if share > allowed_fraction and not success
        ]
        result_problems = list(rollout.problems)
        if gamed_environments:
            result_problems.append(
                f"{len(gamed_environments)} environments exceed the oracle-return allowance without completing the task"
            )
        results[pattern] = {
            "return_mean": earned,
            "fraction_of_oracle": fraction,
            "task_succeeded": all(succeeded),
            "success_fraction": _mean([1.0 if value else 0.0 for value in succeeded]),
            "gamed_environment_count": len(gamed_environments),
            "terminal_steps": rollout.terminal_steps,
            "terminal_success": rollout.terminal_success,
            "component_means": {name: _mean(values) for name, values in rollout.components.items()},
            "status": "pass" if not result_problems else "blocked",
            "reason": "; ".join(result_problems),
        }
        problems.extend(f"{pattern}: {problem}" for problem in result_problems)
    metrics = {
        "seed": seed,
        "steps": steps,
        "oracle_return_mean": _mean(oracle_returns),
        "allowed_fraction_of_oracle": allowed_fraction,
        "patterns": results,
    }
    return {
        "probe": "gaming",
        "status": "pass" if not problems else "blocked",
        "reason": "; ".join(problems),
        "metrics": metrics,
    }


def component_dominance(component_means: dict[str, float], bound: float) -> tuple[bool, str]:
    """Check the largest positive component against the recorded reward bound."""

    if not isinstance(bound, (int, float)) or isinstance(bound, bool) or not 0 < float(bound) <= 1:
        return False, "component dominance bound must be greater than zero and no greater than one"
    if not isinstance(component_means, dict) or not component_means:
        return False, "component dominance needs observed reward components"
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))
        for value in component_means.values()
    ):
        return False, "component dominance contains a non-finite reward component"
    positives = {name: value for name, value in component_means.items() if value > 0}
    total = sum(positives.values())
    if total <= 0:
        return False, "component dominance needs at least one positive observed reward component"
    name, value = max(positives.items(), key=lambda item: item[1])
    share = value / total
    if share > bound:
        return (
            False,
            f"reward component {name} contributes {share:.0%} of positive return, above the dominance bound {bound:.0%}",
        )
    return True, ""


def penetration_depths_from_separations(separations: list[list[float]]) -> list[float]:
    """Convert PhysX contact separations to maximum penetration depth per environment."""

    depths: list[float] = []
    for env_separations in separations:
        if not _finite_all(env_separations):
            raise ValueError("contact separation contains a non-finite value")
        depths.append(max((max(0.0, -float(value)) for value in env_separations), default=0.0))
    return depths


def assemble_probe_report(
    *,
    bindings: dict[str, Any],
    runtime: dict[str, Any],
    execution: dict[str, Any],
    probes: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    status = (
        "pass"
        if not errors
        and len(probes) == 5
        and {str(item.get("probe") or "") for item in probes} == {"smoke", "oracle", "reset", "repeat", "gaming"}
        and all(item.get("status") == "pass" for item in probes)
        else "blocked"
    )
    return {
        "report_identity": {"id": PROBE_REPORT_ID, "version": PROBE_REPORT_VERSION},
        "protocol_identity": {"id": PROBE_PROTOCOL_ID, "version": PROBE_PROTOCOL_VERSION},
        "execution_identity": dict(execution),
        "status": status,
        **bindings,
        "runtime": dict(runtime),
        "probes": list(probes),
        "errors": list(dict.fromkeys(errors)),
    }
