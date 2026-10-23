# Engineering defaults

- Use the installed Ponytail skill in **full** mode for every coding, design, review, refactoring and dependency task. Keep it active unless the user says "stop ponytail" or "normal mode".
- Trace callers, configuration and tests before editing. Prefer existing helpers, the standard library and the smallest complete change. Remove verified dead code and duplicated work; preserve explicit requirements and validation.
- Keep the AI implementation in `ai_paper/` and regression checks in `tests/`. Preserve the root CLI filenames and dependency-free core.
- Keep the original offline commands and saved journals compatible. Share exact accounting in `paper_trader.settle_fill`; prices use verified native integer units and cash uses integer paise.
- Broker execution endpoints remain inaccessible. Keep credentials, recordings, datasets, journals and model artifacts outside Git. Training dependencies remain optional for the simulator.
- Run `python -m unittest -v` after behavioral changes, using the project environment when available. Report optional dependency/CUDA skips separately from passing tests and distinguish local checks from authenticated broker, performance and remote-CI acceptance.
- Read `AI_WORKFLOW.md` for the implemented workflow and admission gates. Do not weaken those gates to make a demo or test pass.
