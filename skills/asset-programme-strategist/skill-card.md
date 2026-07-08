# Asset-programme-strategist

Domain: intake

Mission: Turn a natural-language asset brief or an existing run request into a validated run-request JSON, then enter the whole-run agentic path.

Inputs: a partial run-request draft derived from the natural-language brief and source references or an existing run-request JSON object, plus focused answers to start-blocking questions.

Outputs: validated run-request JSON, named missing inputs when blocked, pending evidence, routed stages, intake report and factory start result.

Tools: `asset_programme_intake`, then `asset_factory_start`.

Boundary: exact Profile ID and version are mandatory only for SimReady, USD-package and RL outputs. They are never invented. Physics-only work needs no Profile and may start as a fail-closed proposal with signed evidence pending. Native STEP and IGES require a converted USD or supported mesh source.
