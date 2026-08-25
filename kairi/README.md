# kairi — the search-law gate

This directory holds the **search law**: the hard rules that decide which job
postings are worth pursuing, written in a form a computer can check.

## Why the rules live as data

Until now every rule about which jobs to chase (which cities count, what salary
is low enough to skip) lived inside instruction documents — prose that an AI
assistant reads and is *trusted* to obey. Prose cannot enforce anything; it can
only be remembered. This directory changes that: the rules become a small JSON
file (`search-law.json`) that a real program — `gate.py` — reads and applies,
pass/fail, before any job is scored or ranked. If a posting breaks a hard rule,
it is rejected by code, not by a judgment call.

## Where the real file lives (and why it is not in this repository)

**The real rules file is NOT stored here.** This repository is public. Your
choice of cities and your salary floors are personal information — committing
them would publish them to anyone who finds the repo.

So the gate looks for the real file **outside** the repository, at:

    ~/.local/share/kairi/search-law.json

What is stored here instead:

- `search-law.example.json` — a copy of the file's shape, filled with obviously
  fake placeholder cities and round example numbers. Use it as a template.
- `README.md` — this file.
- `gate.py` — the program that enforces the rules.

`kairi/search-law.json` is listed in the repo's `.gitignore`, so even if you
experiment by placing a filled-in copy inside this directory, git will refuse
to track it.

## How to set up your own search-law.json

1. Copy the example somewhere outside the repository:

       mkdir -p ~/.local/share/kairi
       cp kairi/search-law.example.json ~/.local/share/kairi/search-law.json

2. Open the copy in any text editor and replace the placeholders:
   - put your real cities into the `cities` lists,
   - adjust the floors if your numbers differ,
   - set `currency` to the currency your salaries are stated in.
3. Save. That is the whole installation — the gate reads exactly that one file.

## What each field means

| Field | Meaning |
|---|---|
| `version` | A label for this edition of your rules. Bump it when you change them; run reports record which version they applied. |
| `ruled` | The date you last changed the rules (YYYY-MM-DD). Purely a record. |
| `currency` | The currency your salary floors are written in, e.g. `"USD"`. A posting that states its salary in any other currency is **not** converted — the gate marks it "unknown" and hands it to you, because converting would mean guessing. |
| `groups` | Your location groups. Each group has a `name`, a list of `cities`, and a `soft_floor`: the annual base salary below which a posting there gets flagged as under-paying — but still passed on for review, never auto-rejected. |
| `remote.soft_floor` | The same idea for remote roles: postings flagged below it, never rejected because of it. |
| `remote.precedence_over_groups` | If `true`, a remote role is judged against the **remote** floor no matter which city the employer sits in — a remote role is a remote role wherever the company happens to be. If `false`, a remote posting whose city belongs to one of your groups is judged against **that group's** floor instead; a remote posting matching no group still uses the remote floor. |
| `absolute_floor` | The one hard number. A posting whose **stated top-of-range salary** falls below this is rejected outright, in every category, with no exceptions. Below this line: no application. |
| `posture` | Free text describing how borderline roles should be treated; the gate records it in the artifact but does not parse it. |

Two things the floors deliberately do **not** do:

- **Most postings state no salary at all.** That is normal, not a failure. An
  unstated salary is recorded as unknown and the posting passes on to review.
  Only a *stated* figure below `absolute_floor` causes a rejection.
- The group/remote floors are soft. Under them a posting is *flagged* with how
  far short it falls — the decision stays yours.

## What the gate refuses to do

If `~/.local/share/kairi/search-law.json` is missing or unreadable, **the gate
stops with an error rather than running with guessed rules.** It will name the
path it looked for and point you back to this README. A gate that invents its
own thresholds when the real ones are absent would be worse than no gate —
silently enforcing numbers nobody chose.
