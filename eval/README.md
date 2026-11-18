# Evaluation harness

The benchmark runs as an [Inspect](https://inspect.aisi.org.uk) evaluation, the open-source framework of the UK AI Security Institute.

| File | Purpose |
|---|---|
| `../src/legal_reasoning_bench/legal_reasoning_bench.py` | Inspect task `legal_reasoning_bench`. It loads each case as a sample, runs the model and scores the answer with model judges against the case rubric |
| `rescore.py` | Grades saved answers again without regenerating them, and reuses grades that already exist |
| `apply_review.py` | Applies the author's review decisions and computes the final scores |
| `report.py` | Writes answers, per-criterion grades and a summary as Markdown |
| `run.py` | Entry point for the GitHub Actions workflow |

## Running locally

```bash
uv sync                                 # or: pip install -e .
sudo apt-get install poppler-utils      # optional: better PDF text extraction
export OPENROUTER_API_KEY=...

uv run inspect eval src/legal_reasoning_bench/legal_reasoning_bench.py@legal_reasoning_bench \
  --model openrouter/<lab>/<model> \
  --reasoning-effort high
```

Task options (`-T`):

| Option | Values | Default |
|---|---|---|
| `cases` | Comma-separated case IDs, for example `uk-val-001,co-fx-001` | all |
| `web` | `search` (official-source web search through OpenRouter; OpenRouter models only) or `none` | `search` |
| `judges` | Comma-separated judge models, or `none` to skip scoring | the three round-1 judges |
| `attempts` | Maximum attempts when a provider returns an empty or truncated answer | 5 |

Open the logs with `inspect view`.

## Scoring

- Each judge returns an award of 0, 0.5 or 1 for every criterion, with a reason.
- A case score is positive points plus penalty deductions. Deductions are capped at −30, and the score is bounded between 0 and 100.
- The official score is the mean of the judges from developers other than the one whose model is graded. Self-grades are stored for the self-preference analysis.

## Running on GitHub Actions

`.github/workflows/regbench.yml` runs when `eval/run_request.json` changes on `main`, and can also be started from the **Actions** tab. It needs a repository secret named `OPENROUTER_API_KEY`. See `run_request.example.json` for the request format. Results are committed to `eval/results/<run_id>/`.
