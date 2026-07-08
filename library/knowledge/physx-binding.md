# PhysX and physical property binding

How physical properties become USD physics opinions in this factory, and which schema names are legal.

## UsdPhysics schemas

Physics in OpenUSD is declared through the UsdPhysics schema family; PhysX consumes these and adds engine-specific extensions under PhysxSchema.

- `UsdPhysicsRigidBodyAPI` on a body prim: `physics:rigidBodyEnabled`, `physics:kinematicEnabled`, `physics:startsAsleep`, `physics:simulationOwner`.
- `UsdPhysicsCollisionAPI` on collision prims: `physics:collisionEnabled`; `UsdPhysicsMeshCollisionAPI` sets `physics:approximation` (none, convexHull, convexDecomposition, boundingCube, boundingSphere, sdf). Convex hull is the default proposal for compact rigid parts; concave functional openings (a handle a gripper must pass through) need convexDecomposition or an explicit gap justification.
- `UsdPhysicsMassAPI`: `physics:mass`, `physics:density`, `physics:centerOfMass`, `physics:diagonalInertia`, `physics:principalAxes`. Author mass or density, not contradictory values of both.
- `UsdPhysicsMaterialAPI` on a material prim: `physics:staticFriction`, `physics:dynamicFriction`, `physics:restitution`, `physics:density`. Physics materials bind through `material:binding:physics`.

## Joints and articulations

- Joint prims: `UsdPhysicsRevoluteJoint`, `UsdPhysicsPrismaticJoint`, `UsdPhysicsSphericalJoint`, `UsdPhysicsFixedJoint`, `UsdPhysicsDistanceJoint`, each with `physics:body0`, `physics:body1`, local positions and rotations, `physics:axis`, `physics:lowerLimit`, `physics:upperLimit`.
- Drives attach through `UsdPhysicsDriveAPI:<dof>` with target, stiffness and damping.
- `UsdPhysicsArticulationRootAPI` marks the root of a reduced-coordinate articulation; robots and jointed assets get exactly one per articulation tree.
- Collision filtering between adjacent articulated parts uses `UsdPhysicsFilteredPairsAPI`.

## How this factory binds properties

1. Stage 3 (material inference) proposes physical values per material class with units, ranges, uncertainty and evidence, drawing priors from the library's physical property dictionary.
2. Stage 5 (physics-articulation) consumes only validated or review-approved proposals and authors the UsdPhysics opinions into `phy.usda` and `art.usda`.
3. Every authored numeric value carries provenance back to the proposal record; the numeric-physics-review gate blocks release until a reviewer accepts task-critical values.
4. Grasp affordances are recorded in the physics-articulation manifest and surfaced as semantics; they are not physics schema opinions.

## Grounding rule

Use only the schema and attribute names listed here or read from an inspected stage. Property values come from the physical property dictionary, measurements, specifications or reviewer decisions, cited by evidence id. A value with no citation is a defect.
