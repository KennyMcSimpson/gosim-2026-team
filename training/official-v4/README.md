# Official v4 L1–L4 Training Cards

This folder holds the four public local practice cards, L1–L4, and the GOSIM organizer-provided v4 runner. It is the fixed training and regression set for the team. These cards are distinct from the alpha–delta cards used by the practice application and from any later seed-generated simulations.

The card data, including scorer truth, and the runner are copied from the GOSIM 2026 Agentic Observer examples archive supplied for this project. The upstream files are preserved; locally generated Python bytecode is omitted. The organizer license is retained in LICENSE.md and covers these materials under CC BY-NC 4.0. The runner manifest and verification script are retained under runner/.

## Run a card

From the repository root, with Python 3.9 or newer and an agent that speaks the v4 JSONL protocol:

~~~sh
python3 training/official-v4/runner/run_local.py --card L1 --agent "python3 agent.py" --agent-cwd path/to/your/agent
~~~

Replace L1 with L2, L3, or L4. The report and detailed run logs are written by the official runner to its output directory. Local scores are for training and regression only; they are not alpha–delta calibration results or official platform scores.

To check the copied scoring engine against its source manifest:

~~~sh
python3 training/official-v4/runner/verify_engine.py
~~~

## Separation rule

Keep L1–L4 training and regression runs here. Keep the application's four fixed alpha–delta calibration environments in the separate practice-app repository. Keep seed-generated alpha-like variants in their own simulator mode and label them synthetic. Do not use a seed to redefine or overwrite an L1–L4 card or a fixed alpha–delta calibration card.