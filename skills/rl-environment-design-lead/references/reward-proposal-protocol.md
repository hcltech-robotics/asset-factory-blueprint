# Reward acceptance protocol

The implemented reward contains exactly `reach`, `lift` and `action_rate` in the forms recorded by the environment schema. Different components or formulae are unsupported.

A passing runtime report contains:

1. finite zero-action and random-action smoke results;
2. a fully successful grasp-conditioned oracle with no joint-limit violations;
3. finite observed component returns and a passing dominance audit;
4. repeatability within the recorded trajectory and return tolerances; and
5. all four gaming patterns run for the complete scripted-oracle horizon and remain within the per-environment oracle-return allowance.

Runtime evidence does not approve the reward. It moves the environment to review once the other deterministic and evidence gates pass.
