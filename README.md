# Symptom Dataset Agent

Generate a structured symptom severity dataset for any disease. The agent searches the web, prioritizes trusted medical sources, extracts clean text, and uses OpenRouter to normalize and grade symptoms into five severity levels.

## How it works

1. Search the web for disease-related medical pages.
2. Crawl and extract readable text from high-quality sources.
3. Ask OpenRouter to return symptoms in Bangla then English with severity levels.
4. Deduplicate, normalize, and append to a single CSV.

## Setup and run

Use a virtual environment (recommended). If you prefer the global environment, skip the venv steps.

### Windows

Create and activate a venv:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Set your OpenRouter API key:

CMD:
```bat
set OPENROUTER_API_KEY=YOUR_KEY
```

PowerShell:
```powershell
$env:OPENROUTER_API_KEY="YOUR_KEY"
```

Run the agent:

```powershell
python symptom_dataset_agent.py "fever"
```

### Linux (and macOS)

Create and activate a venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Set your API key:

```bash
export OPENROUTER_API_KEY="YOUR_KEY"
```

Run the agent:

```bash
python symptom_dataset_agent.py "fever"
```

The output CSV is appended to `adata.csv` in the current folder and written as UTF-8 with BOM for Excel compatibility.

## Output format

The CSV includes these columns:

| disease_bn | disease_en | symptom_bn | symptom_en | severity_level | severity_label | action_mild_bn | action_mild_en | action_severe_bn | action_severe_en |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ... | fever | ... | headache | 1 | mild | ... | ... | ... | ... |

Notes:

- Bangla values come first, followed by English.
- Severity levels are 1 to 5 with labels: mild, normal, moderate, severe, critical.
- Duplicate symptoms are removed per run; new runs append rows to the same file.

## Common flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--min-symptoms` | 20 | Target number of unique symptoms |
| `--max-pages` | 20 | Max pages per query |
| `--max-depth` | 1 | Crawl depth |
| `--search-limit` | 10 | Search results per query |
| `--concurrency` | 5 | Concurrent requests |
| `--max-chars` | 6000 | Max chars per model chunk |
| `--timeout` | 25 | Request timeout seconds |
| `--max-retries` | 3 | Retry count |
| `--min-text-len` | 500 | Min page text length |
| `--output-dir` | . | Output folder |
| `--output-file` | adata.csv | Output CSV filename |
| `--model` | tencent/hy3-preview:free | OpenRouter model id |

Example:

```bash
python symptom_dataset_agent.py "asthma" --min-symptoms 30 --max-pages 30
```

## Tips

- Use quotes for multi-word diseases, e.g., `"heart failure"`.
- If results are sparse, increase `--max-pages` or `--search-limit`.
- You can pass the API key directly with `--api-key` instead of the env var.