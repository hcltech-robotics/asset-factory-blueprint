---
description: "Read the theoretical basis for task-relative fidelity, evidence, physical interaction, OpenUSD composition, agentic authoring and learning environments in Asset Factory Blueprint."
---

# Theory

A simulation asset is an authored object model that becomes executable through composition and runtime interpretation. Its adequacy depends on the task, robot, sensor configuration and simulation runtime. Asset Factory Blueprint (AFB) constructs, tests and varies these models while retaining their evidence and authoring decisions.

The Theory section supplies the reasoning behind the operational blueprint. These chapters explain why reconstruction produces hypotheses rather than facts, why geometry and physical properties need different evidence, why visual and collision representations serve different computations and how OpenUSD composition determines the effective model.

## Reading order

1. [Task-dependent fidelity](task-dependent-fidelity.md) defines adequacy relative to tasks, policies and an experimental context.
2. [Reconstruction and evidence](reconstruction-and-evidence.md) treats asset creation as an inverse problem with observed, inferred and unresolved properties.
3. [Geometry, materials and physical interaction](physical-interaction.md) relates surface representations to mass, contact, articulation and sensing.
4. [Composition and reproducibility in OpenUSD](composition-and-reproducibility.md) explains how separately authored claims become a reproducible executable scene.
5. [Agentic asset construction](agentic-construction.md) defines a bounded process for proposal, inspection and repair with explicit stopping conditions.
6. [From assets to learning environments](learning-environments.md) separates object uncertainty, population variation, deliberate training design and held-out evaluation.

The chapters share definitions but remain independently readable. Operational commands, schemas and promotion rules remain in the [Blueprint](../blueprint.md), [pipeline](../pipeline/00-intake-and-sources.md) and [platform](../platform/orchestrator.md) documentation.

## Notation

| Symbol | Meaning |
|---|---|
| \(e\) | available observations, measurements and specifications |
| \(\phi\) | object parameters, including geometry, material state, mass properties and articulation |
| \(\kappa\) | varying scene and sensor conditions, including layout, lighting and observation noise |
| \(\theta=(\phi,\kappa)\) | complete set of sampled environment parameters |
| \(A\) | authored asset package representing a set of modelling choices |
| \(c\) | fixed experimental context: embodiment, sensor interface, solver, timestep and runtime configuration |
| \(\tau\) | task definition and success criterion |
| \(\pi\) | policy |
| \(p(\phi\mid e)\) | posterior belief where an explicit probabilistic model supports one |
| \(q_{\mathrm{train}}(\theta)\) | distribution deliberately selected for training |
| \(q_{\mathrm{deploy}}(\theta)\) | deployment conditions estimated from the target population and operating process |
