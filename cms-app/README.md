# CMS app: from a website to agent tools

We take the GUC student systems and turn them into something an agent can use.
The work is in four small notebooks that teach one idea each.

| Notebook | The move | The agent can... |
|---|---|---|
| `01-files-into-context.ipynb` | read a file's **content** into the chat | talk about what is *inside* a PDF or Excel file |
| `02-download-files.ipynb` | **download** the file to disk | save files, but not read them |
| `03-grades-from-the-portal.ipynb` | read your **grades** from the portal | answer about your marks and weakest quiz |
| `04-quiz-review-combined.ipynb` | **combine both systems** | find your worst quiz, open its model answer, explain the topic |

They sit on two tiny packages that each log in and hand back plain data, knowing
nothing about agents:

- `guc_cms/` : the CMS (courses, files, file bytes)
- `guc_portal/` : the student portal / SIS (transcript, quiz and assignment marks)

Wrapping their methods as tools is what we do in the notebooks. Notebook 4 is the
payoff: one agent using tools from *both* packages at once.

A note on the portal: it is old and slow, and rate-limits if you rush it (about
one request a minute). The package waits and retries on its own; just be patient
with notebooks 3 and 4 in class.

## Why two notebooks

They are the same app with one line changed, on purpose. The point is the
*difference*:

- Notebook 1 puts content **in context**. Great for questions and summaries, but
  every file costs tokens, so it does not scale to many or large files.
- Notebook 2 puts a file **on disk**. Almost free, but the agent never sees the
  content.

That gap is exactly why the next step is a **backend**: a place the agent can keep
files and open them on demand, instead of carrying everything in the conversation.

## Setup

```bash
uv sync                       # make the venv, install everything (uses Python 3.12)
cp .env.example .env          # then paste your ANTHROPIC_API_KEY into .env
uv run jupyter lab            # open the notebooks
```

When a notebook runs, it asks for your GUC username and password (hidden input,
never written to the notebook).

## The package in one screen

```python
from guc_cms import GucCms

cms = GucCms()                          # GUC_USERNAME / GUC_PASSWORD from env, or pass them
cms.list_courses()                      # -> [Course(code, title, id, season_id), ...]
cms.find_course("software engineering") # -> one Course
content = cms.get_content(course)       # -> files, grouped by week: content.weeks()
cms.fetch_bytes(item)                   # -> raw bytes, nothing saved (used by notebook 1)
cms.download(item, "downloads")         # -> saves to disk, returns the path (notebook 2)
```

## A note on the login

The CMS runs on IIS with Windows (NTLM) authentication, the same box your browser
pops up. `requests_ntlm` does that handshake, so after logging in it is ordinary
HTTP underneath.

Never hard-code your password in a cell you might screen-share. The notebooks read
it with `getpass` for this reason.
