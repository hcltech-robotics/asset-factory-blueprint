---
description: "Build an Isaac Lab task contract from a validated asset package while carrying recorded asset uncertainty into training and evaluation."
---

# RL environment

This downstream extension takes a validated asset package and produces an Isaac Lab RL task with complete evidence lineage. It runs only after stage 7 simready-verification and the target runtime's `isaac-load` check.

![rl environment loop](../assets/rl-environment-loop.svg)

## Variant distributions

A policy learns from its observations and the effects of its actions. Geometry, texture variation, materials, contact behaviour and articulation shape that signal. An asset can look correct in a catalogue render while remaining unsuitable for grasping or pushing, or leaving joint-limit behaviour unspecified. The upstream stages make these properties explicit, measured and reviewable.

Existing systems generate simulation tasks, assets and rewards from language and image models [@wang_robogen_2024; @katara_gen2sim_2024]. Others build procedural houses [@deitke_procthor_2022] or reconstruct articulated objects from interaction and single images [@jiang_ditto_2022; @chen_urdformer_2024], supported by part-level datasets and affordance models [@xiang_sapien_2020; @mo_where2act_2021]. The cited systems do not provide a record of each asset's known properties, uncertainty and approvals. Asset Factory Blueprint carries that record into environment design.

The base asset supplies a prior over valid scenes rather than a single training target, following the same premise as ConTaRo. In the digital-cousins study, policies trained on affordance-preserving scene variants achieved 90% zero-shot success on the same manipulation task, compared with 25% for policies trained on the exact digital twin [@dai_cousins_2024]. This lane defines the variant model, admissible changes, evidence-backed bounds and the training evidence required for promotion.

## Training contract

Training over asset variants is modelled as a contextual Markov decision process: one task with a family of transition functions indexed by mass, friction, drive stiffness, texture, placement and other context values [@hallak_contextual_2015]. When the policy cannot observe that context directly, the problem becomes an epistemic POMDP. Most generalisation failures can then be understood as partial observability of the context [@ghosh_epistemic_2021; @kirk_generalisation_2023].

The asset factory records the prior over that context:

- Every physical property proposal carries a value, unit, range, distribution, method and confidence.
- Every authored mass carries a sealed uncertainty record.
- Every grasp point carries a frame, an approach vector, a gripper width and a confidence.

The lane uses these fields as context priors. It records their source manifest identifiers and checksums in the environment manifest, then derives the training distribution from the recorded uncertainty.

## Upstream records

| Upstream record | Fields consumed | Use in the environment |
|---|---|---|
| `simready-asset-manifest` | package identity, layer stack, units, axis policy, Profile, promotion status | scene composition root and lineage |
| `physics-articulation-manifest` | `affordances.grasp_points`, `affordances.affordance_labels` | reset distributions, grasp-conditioned rewards, reachability gate |
| `physics-articulation-manifest` | `joints`, `drives`, `limits`, `articulation_roots` | action space for articulated tasks, limit terminations |
| `physics-articulation-manifest` | `mass_properties`, `physics_materials`, sealed uncertainty record | dynamics randomisation ranges with `evidence` provenance |
| `physics-articulation-manifest` | `tuning_scenarios`, `validation_scenarios` | smoke tests and scripted-oracle scenarios |
| `material-inference-manifest` | `physical_property_proposals` (value, unit, range, distribution, confidence) | context prior for vEp, friction &c. |
| `nonvisual-material-manifest` | thermal, acoustic and electrical values with evidence | nonvisual observation channels |
| `isaac-runtime-evidence` | `physics_dt`, `settle_steps`, `repeatability_tolerance_metres`, `runtime_identity.physics_backend` | timestep and backend binding, reset feasibility, rollout repeatability |
| `asset-layout-manifest`, `mutation-plan`, `variants.usda` | placements, parametric patterns, gated operations with rollback notes | layout randomisation and the editing curriculum |
| `task-fitness-protocol` | `scope: articulated_training` tests, metrics and tolerances | emitted by this lane, evaluated by the evaluation stage |

## How to use this lane

Agent skill: `rl-environment-design-lead`. The orchestrator invokes it only after the asset package has passed the stage 7 gates, including the `isaac-load` gate where the runtime is configured.

Declare the target behaviour: picking, placing, pushing, opening, closing, inspection or navigation. The skill checks that behaviour against the available asset evidence. Missing grasp points block a pick task; missing joint limits or drives block an opening task; and a missing friction range blocks a push task. The skill does not approximate missing records. Grasp tasks reuse the grasp affordances recorded in the physics-articulation manifest rather than re-deriving them from geometry.

The environment manifest records:

- the runtime identity the environment is bound to
- what the robot observes, and which of those observations are privileged
- which actions it can take and from which recorded joints and bodies
- what earns reward and which probes the reward passed
- what ends an episode and how episodes reset
- which variants are allowed during training and where each bound came from
- how the environment is evaluated and which validation run proves it starts cleanly

## Environment identity

Isaac Lab is the canonical RL environment framework for this lane. It succeeds Isaac Gym and ORBIT [@makoviychuk_isaacgym_2021; @mittal_orbit_2023] and supports Newton alongside PhysX, Warp and MuJoCo [@mittal_isaaclab_2025; @nvidia_isaaclab_newton_2026]. The same asset can produce different contact behaviour and failure modes under different solvers [@hu_simweaver_2026]. The manifest therefore treats the physics backend as part of environment identity: a PhysX pass does not validate a Newton environment.

The timestep is part of the same identity. A manager-based Isaac Lab environment is defined by `sim.dt`, `decimation` and `episode_length_s` [@nvidia_isaaclab_manager_env_2026]. Isaac runtime evidence records the `physics_dt` used to check the asset's contact behaviour. The training timestep must match it unless fresh runtime evidence is attached. Seeds, environment count and Isaac Lab version complete the identity record.

## Observations and privilege

Observation terms are split into two groups. The `policy` group contains proprioception and the sensor channels available at deployment, each with its noise model. The `critic` group may also contain ground-truth state that the deployed policy never sees. Asymmetric actor-critic training [@pinto_asymmetric_2018], learning by cheating [@chen_cheating_2020] and teacher-student distillation [@lee_terrain_2020; @kumar_rma_2021] use this privileged information, usually through simulator internals.

Every privileged input in this blueprint must come from a validated manifest field with evidence and a checksum. Each `critic` term cites the manifest path from which it is read.

The manifest also declares an `adaptation_mode`:

- `robust`: the policy averages over the context prior and carries no explicit estimate of it.
- `adaptive`: the policy infers the context online from its own observations, in the manner of rapid motor adaptation [@kumar_rma_2021].

Adaptive mode requires an identifiability audit. For each randomised parameter, the manifest records which `policy` observations can distinguish its values within one episode. Lifting can identify mass, pushing can identify friction, and joint motion can identify drive damping. Proprioception cannot identify texture variation. A randomised parameter that the policy cannot identify from its observations is handled robustly regardless of the declared mode, and the audit records that decision.

## Sensor contract

For every channel, the sensor contract records the sensor model, noise model and parameters, update rate relative to `decimation`, and supporting upstream evidence. Isaac Lab's observation corruption and noise configuration is the rendering target for visual and proprioceptive channels [@nvidia_isaaclab_manager_env_2026].

Stage 6 grounds thermal, acoustic and electrical observations in the nonvisual materials manifest. A thermal channel, for example, cites the emissivity and temperature records from which it renders. Channels without supporting records are excluded from the `policy` group.

## Actions, resets and terminations

Action terms are derived from the physics articulation manifest, never from geometry alone. Joint-space actions name the recorded joints, drive mode and limits. Task-space actions name the body they affect and the workspace bounds from the safety record. Manifest validation rejects commands beyond a recorded joint limit.

Reset distributions sample initial poses, joint positions and object placements within recorded bounds. Grasp tasks may seed resets from recorded grasp points. Every reset distribution carries feasibility evidence: sampled resets must settle without interpenetration within the runtime evidence's `settle_steps`, using its settled-speed threshold as the criterion.

Terminations include time-out, success, recorded joint-limit violation, illegal contact and workspace exit. The task-fitness definition is the sole success criterion.

## Rewards

Every reward remains a proposal until it passes the probes, whether it came from an engineer or a language model. In the Eureka pattern, the model writes reward code, trains policies, receives training statistics and revises the reward [@ma_eureka_2024; @yu_l2r_2023; @xie_text2reward_2024]. DrEureka adds a safety instruction and reports that this makes the rewards deployable [@ma_dreureka_2024]. Each iteration in this blueprint writes a proposal record, an evidence record and a report. Promotion requires both probe evidence and review.

!!! sidebar "Rigid Kriegsspiel before free Kriegsspiel"

    The Prussian war game began in 1824 as a rules-heavy exercise, played with dice and printed tables that fixed the outcome of every engagement [@reisswitz_kriegsspiel_1824]. Half a century later it split in two: rigid Kriegsspiel, governed by the tables, and free Kriegsspiel, governed by an umpire's judgement, which Verdy du Vernois argued was the only way to keep the game close to war [@verdy_kriegsspiel_1876]. The Prussian General Staff kept both, in that order. The tables caught the errors an umpire would be too generous to notice.

    Reward review follows the same order: deterministic probes precede reviewer judgement. The probes catch errors that an informal review may excuse.

Before review, every reward proposal passes the following deterministic probes, and the manifest records the result of each:

- **Random-policy baseline.** Mean return and per-component magnitudes under uniformly random actions over the recorded seeds.
- **Scripted-oracle bound.** A scripted policy built from the recorded affordances: move to the grasp frame along the approach vector, close to the recorded width, lift; or drive the joint through its recorded range. The oracle's return bounds what the task is worth, and its success proves the task is feasible for the embodiment.
- **Component audit.** No component dominates the total by construction; every component has units or is dimensionless by construction; shaping terms are potential-based where the task admits it, so that they cannot change the optimal policy [@ng_shaping_1999].
- **Specification-gaming probes.** Known patterns of reward hacking [@amodei_concrete_2016; @skalse_hacking_2022; @pan_misspecification_2022] checked against the proposal: proximity rewarded without contact, velocity rewarded without displacement, termination that pays, progress that can be earned by oscillation.
- **Evidence rule.** No reward term depends on a quantity whose upstream record is `proposal` or `review_required`.

A proposal that fails a probe returns to the proposer with the probe output. A proposal that passes them all becomes `review_required` and goes to the reviewer with the probe evidence attached.

## Randomisation with provenance

Domain randomisation began with visual variation [@tobin_domain_2017] and later covered dynamics [@peng_dynamics_2018]. Adaptive methods let the range grow with the policy [@openai_rubiks_2019], while canonicalisation maps randomised observations back to a reference appearance [@james_rcan_2019]. Surveys cover the resulting field [@zhao_survey_2020; @muratore_review_2022; @aljalbout_realitygap_2025]. Ranges that are too broad can drive the optimiser into a conservative local optimum; ranges that are too narrow fail to generalise [@muratore_review_2022; @chen_understanding_2022].

Real-robot data provides another source of bounds. SimOpt and BayesSim infer a posterior over simulator parameters from real rollouts [@chebotar_simopt_2019; @ramos_bayessim_2019], and DROPO performs offline inference from logged trajectories [@tiboni_dropo_2023]. Active domain randomisation and entropy maximisation expand the distribution within policy tolerance [@mehta_active_2020; @tiboni_doraemon_2024]. DrEureka first perturbs the simulator around a trained policy to measure those tolerances, then uses a language model to set the ranges within them [@ma_dreureka_2024].

This blueprint also derives bounds from the asset's evidence. Every randomisation axis records its provenance.

| Provenance | Meaning | Where it comes from |
|---|---|---|
| `evidence` | interval taken from an upstream record | material and physics manifests, the sealed uncertainty record |
| `feasibility` | interval a trained policy tolerates | a parameter sweep around the smoke-trained policy |
| `posterior` | interval inferred from real rollouts | BayesSim or DROPO style inference, when real data exists |
| `policy_default` | declared fallback | the randomisation policy in the run request |

The manifest records how these sources combine into each training range. If the evidence and feasibility intervals do not overlap, the axis becomes `review_required`. The mismatch may indicate that the policy cannot tolerate the measured asset range or that the asset evidence needs review; the lane does not choose between them.

!!! sidebar "Requisite variety, and the good regulator"

    Ashby's law of requisite variety states that a regulator can only hold a system within bounds if the variety of its responses matches the variety of the disturbances it faces [@ashby_cybernetics_1956]. Domain randomisation is that law made operational: the disturbances are the contexts the policy will meet, and the recorded uncertainty in the manifests is a measured lower bound on their variety. A randomisation range narrower than the evidence interval is a regulator that has been told less than is known about its adversary.

    Conant and Ashby went further and proved that every good regulator of a system must be a model of that system [@conant_regulator_1970]. That is the cybernetic case for `adaptive` mode over `robust` mode: a policy that carries an estimate of the context is a better regulator than one that averages over it, provided the context is identifiable from what the policy can observe. The identifiability audit exists to check that proviso.

Variants must preserve recorded affordances. An affordance depends on the object and the agent together [@gibson_ecological_1979]. A texture change that makes a handle unrecognisable or a deformation that moves a grasp point beyond the gripper's reach changes the task and is rejected. Only variants that pass this check enter the training distribution as digital cousins [@dai_cousins_2024].

## Curriculum tiers

Domain randomisation is the simplest form of unsupervised environment design [@dennis_paired_2020]. Prioritised level replay selects high-regret levels from random generation [@jiang_plr_2021; @jiang_replay_2021]. ACCEL edits high-regret levels so complexity can compound [@parkerholder_accel_2022], while POET co-evolves environments and agents [@wang_poet_2019]. Grounded curriculum learning keeps the curriculum tied to the real deployment task distribution [@wang_gcl_2024].

ACCEL describes each edit as a mutation. The blueprint's mutation plan already declares the target layer, prim, operation, inputs, expected outputs, gates, rollback note and dry-run support. Layout plans add parametric placement patterns with unit policy and bounds. The lane supports three curriculum tiers:

1. **Static.** A declared schedule over randomisation bounds and reset difficulty, in the Isaac Lab curriculum manager idiom, with the terrain-difficulty schedule as the canonical example [@rudin_walk_2022; @portelas_curriculum_2020].
2. **Replay.** Prioritised replay over pre-validated variants, where the replay buffer is a set of variant identifiers with checksums and the priority is an estimated regret.
3. **Editing.** ACCEL-style proposals emitted as mutation plans in `validate_only` mode and promoted only through the existing gates, with rollback notes. The curriculum generator is one more provider whose output is proposal material.

Editing curricula retain lineage, bounds, gates and rollback for every proposed change.

## Affordance-weighted collision fidelity

Collision geometry limits manipulation transfer more directly than visual geometry. One recent study argues that replacing accurate collision meshes with crude convex hulls has a large, underexamined effect on robustness. It measures the error using a surface-sampling distance between the visual and collision shells [@xu_realikea_2026]. Region-specific decomposition tolerances, fine near contact surfaces and coarse elsewhere, cut simulation time by 69% on a pick-and-place task without losing fidelity at contact [@vu_empart_2025]. Bounded-stiffness contact reduction makes tight-clearance insertion learnable [@vuong_contact_2023].

The blueprint records grasp points with frames and approach vectors, together with the moving parts for articulation affordances. The lane measures visual-to-collision shell distance around each grasp point and articulated contact surface, then checks it against the task-fitness tolerance. A value outside tolerance emits a stage 5 request for finer decomposition in the failing region and blocks the lane until that request is resolved. The request uses the collision-aware decomposition already supported by the physics stage [@wei_coacd_2022; @mamou_hacd_2009]. Mandatory mesh verification makes this reliable because approximate convex decomposition behaves poorly on non-manifold input.

## Safety constraints

Safety constraints are recorded as costs in a constrained MDP [@altman_cmdp_1999; @achiam_cpo_2017; @ray_safetygym_2019] and as hard terminations. They are never represented by reward penalties alone. The safety record contains workspace bounds, joint-velocity and torque ceilings from the embodiment description, contact-force ceilings for the asset and robot, and deployment rate limits. DrEureka found that giving the reward proposer a safety instruction made the resulting rewards deployable, so every reward proposal request carries this record [@ma_dreureka_2024].

## Evaluation protocol

Deep reinforcement learning results are sensitive to seeds, and small run counts can mislead [@henderson_matters_2018]. The evaluation record declares the seeds, run count and aggregation method. Unless the protocol authority states otherwise, it uses the interquartile mean with stratified bootstrap confidence intervals [@agarwal_precipice_2021; @patterson_empirical_2024]. Reports break success rates down by held-out variant and randomisation axis.

A policy can learn a proxy that matches the intended goal within the training distribution but diverges outside it [@langosco_misgeneralization_2022; @shah_misgeneralization_2022]. The evaluation record therefore includes a **proxy audit**. Its held-out variants use the mutation machinery to separate candidate proxies from the objective, for example by matching a distractor to the goal colour or placing a handle on the wrong side. Reviewer approval depends on the proxy-audit evidence; the headline metric alone is insufficient.

Isaac Lab Arena is the evaluation harness of record for the Isaac runtime, and its zero-action runner is the reference noop rollout [@nvidia_isaaclab_arena_2026].

## Updating asset evidence

!!! sidebar "The wind tunnel at Dayton"

    In 1901 the Wright glider produced a fraction of the lift its designers had calculated. The calculation rested on the Lilienthal tables and on Smeaton's coefficient of air pressure, a number that had been in circulation since 1759 and that nobody had thought to re-measure. The Wrights built a wind tunnel, measured, and found the coefficient wrong by about a third. The 1902 glider flew [@jakab_visions_1990].

    The episode shows why simulator inputs should be treated as hypotheses. Flight tests provide evidence about those inputs, and the next calculation inherits any error that is not written back into the tables.

Evaluation runs and real-world rollouts provide new evidence about the asset. BayesSim or DROPO-style methods can infer a posterior over the context from those rollouts [@ramos_bayessim_2019; @tiboni_dropo_2023; @aljalbout_realitygap_2025]. The lane writes that posterior as a new physical-property evidence record with lineage to the source rollouts, then proposes a revised asset manifest through the ordinary review gates. This return path lets recorded uncertainty shrink as evidence accumulates.

## Environment card

The manifest renders an environment card containing intended tasks and embodiments, out-of-scope uses, provenance-backed randomisation coverage, known sim-to-real gaps, evaluation results and a reward report [@gilbert_rewardreports_2023]. It serves the same role as a model card or dataset datasheet [@mitchell_modelcards_2019; @gebru_datasheets_2021]. A client's safety function can review it without opening a USD file.

## Inputs

- validated asset package (`manifests/simready-asset-manifest.json`)
- physics-articulation, material-inference and, where present, nonvisual-material manifests
- Isaac runtime evidence for the target backend
- robot embodiment description with joint limits and actuator ceilings
- task objective and success definition
- sensor contract
- randomisation policy and any real-rollout posterior evidence
- safety record

## Process

1. Read the simready-asset-manifest, the upstream manifests and the runtime evidence; verify promotion state and checksums.
2. Reconcile the requested behaviour against the evidence the asset carries; block on missing affordances, limits or ranges.
3. Bind runtime identity: backend, timestep, episode length, seeds, embodiment.
4. Assemble the context prior from the recorded ranges, distributions and uncertainty records.
5. Define observation groups, the sensor contract, actions, resets and terminations from the recorded joints, limits and affordances.
6. Propose rewards, run the deterministic probes and record their results.
7. Derive randomisation ranges with provenance; check affordance invariance per variant; run the identifiability audit when the mode is adaptive.
8. Attach the curriculum tier and, for editing curricula, the mutation-plan contract.
9. Compute affordance-weighted collision fidelity; emit a stage 5 request on failure.
10. Write the evaluation protocol, the `articulated_training` task-fitness protocol and the smoke, train and evaluate command contracts.
11. Emit `manifests/rl-environment-manifest.json`, the environment card and the W&B plan.

## Outputs

- `manifests/rl-environment-manifest.json`
- runtime identity and context prior records
- observation, sensor, action, reset, reward and termination specs with probe evidence
- randomisation spec with per-axis provenance and affordance-invariance results
- curriculum spec and, where applicable, mutation-plan contracts
- `articulated_training` task-fitness protocol
- evaluation protocol with proxy-audit variants
- environment card
- W&B training plan
- smoke, train and evaluate command contracts with digests

## Promotion gates

RL environment generation is blocked until the simready-verification stage has promoted the package with load and physics evidence for the declared backend. Within the lane, promotion requires:

- lineage: every consumed manifest is `validated` or `released` and checksums match
- backend and timestep binding against the runtime evidence
- affordance reachability under the scripted oracle
- affordance-weighted collision fidelity within tolerance
- reset feasibility within the recorded settle criteria
- zero-action smoke rollout, random-policy baseline and oracle bound recorded
- reward component audit and specification-gaming probes passed
- evidence rule satisfied for every reward and observation term
- randomisation provenance complete and affordance invariance checked per variant
- identifiability audit passed when the mode is adaptive
- evaluation protocol with seeds, held-out variants, aggregation and proxy audit
- environment card rendered and checksummed
- W&B plan status, review requirement and promotion decision

## Commands

The manifest stores exact command lines and argument digests for smoke, train and evaluation contracts. The smoke contract steps the composed environment with zero actions and then random actions for a fixed number of frames. Isaac runtimes use the Isaac Lab Arena runner. The train contract names the Isaac Lab entry point, environment identifier, seeds and W&B project. The evaluation contract runs the approved protocol over held-out variants and writes the task-fitness evidence report. The evaluation stage recomputes every result from that protocol.

## References

\bibliography
