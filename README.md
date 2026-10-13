# Safety Course Bot

Moodle (ole.unibz.it) workplace safety courses — automated progress bot.

Attaches to your open Chrome (CDP port **9222**), walks lessons (wait + Next/End), solves quizzes (answer cache + Gemini fallback), and can run **two courses in parallel** in separate tabs.

## Features

- URL-based state machine (`lesson` / `quiz` / `course`)
- Lesson: “Spend at least X minutes” wait, then Next → End
- Quiz: shared `answers_cache.json`, review learning, Gemini only on cache miss
- Parallel courses (same Chrome, two tabs, two workers)
- Never commits API keys (`secrets.env` is gitignored)

## Requirements

- Windows
- Google Chrome
- Python 3.11+
- Gemini API key ([Google AI Studio](https://aistudio.google.com/apikey))

## Setup

```bat
setup.bat
```

Copy secrets:

```bat
copy secrets.env.example secrets.env
```

Edit `secrets.env` and set:

```
GEMINI_API_KEY=your_key_here
```

Courses (edit `config.yaml`):

```yaml
courses:
  - "https://ole.unibz.it/course/view.php?id=13232"
  - "https://ole.unibz.it/course/view.php?id=13250"
```

## Run

1. Close all Chrome windows.
2. Start debug Chrome:

```bat
1_chrome_ac.bat
```

3. Log in to ole.unibz.it in that Chrome.
4. Start the bot:

```bat
run.bat
```

Logs are tagged `[A]` / `[B]` when two courses run in parallel.

## Project layout

| File | Role |
|------|------|
| `bot.py` | Router + parallel course loops |
| `browser.py` | CDP attach, clicks, helpers |
| `lesson.py` | Spend wait + Next/End |
| `quiz.py` | Cache, Gemini, review learn, submit modal |
| `config.yaml` | Courses, waits, model |
| `secrets.env` | Local only (not in git) |

## Safety notes

- Use only on courses you are allowed to automate.
- Do not commit `secrets.env` or real API keys.
- `answers_cache.json` is local and gitignored.

## License

Private use / educational tooling — adjust as needed for your org.
