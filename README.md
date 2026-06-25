# Daily Activity Analyzer

A passive Windows activity tracker that records your computer usage and generates AI-powered summaries of what you did during the day.

## Features

- **Session Recording** — press Start and go about your day; the app tracks foreground window titles, app switches, clicks, and keystrokes
- **Activity Timeline** — see a chronological log of every app you used, how long you spent in each, and what window titles were active
- **AI Analysis** — click "Analyze My Day" to get a natural-language summary ("You started the morning coding in VS Code, then switched to Chrome to research...") plus productivity suggestions
- **Hourly Breakdown** — visual bars showing your activity level by hour
- **Local AI** — runs on Ollama (free, no cloud); supports any model you have installed
- **Data Privacy** — everything stores locally in `~/activity_logs/productivity/`

## Requirements

- Windows 10 or 11
- Python 3.10+
- [Ollama](https://ollama.com/download) (optional — needed for AI analysis)
- Ollama model (e.g. `ollama pull ministral-3:3b`)

## Quick Start

```powershell
py -3 -m pip install pynput pywin32 pillow ollama
py -3 productivity_monitor.py
```

Click **▶ Start Recording**, use your computer normally, then click **■ Stop** and **Analyze My Day**.

## Usage

| Control | What it does |
|---|---|
| ▶ Start Recording | Begin tracking activity |
| ■ Stop | End session, save data |
| Analyze My Day | Generate AI summary + suggestions |
| Model dropdown | Choose an installed Ollama model |
| Quit | Exit the app |

## Data Storage

All data is saved as JSON in `%USERPROFILE%\activity_logs\productivity\`:

- One file per day (`2026-06-26.json`)
- Contains sessions, click/keystroke counts, and hourly breakdowns

## License

MIT
