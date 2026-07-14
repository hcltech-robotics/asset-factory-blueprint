# Task evidence matrix

| Behaviour | Required evidence | Result |
| --- | --- | --- |
| `pick` | accepted asset-local grasp frames, mass and friction; rigid composed USD; fixed-base full-pose URDF with a prismatic gripper | supported |
| any other behaviour | not implemented by the renderer and probe adapter | blocked |

Grasp frames are reused from the physics-articulation manifest. They are not re-derived from geometry.
