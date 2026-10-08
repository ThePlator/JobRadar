# Accessibility

JobRadar should be usable by everyone, including people who use screen readers, keyboard-only navigation, magnification, high-contrast themes or other assistive technology. This page describes what JobRadar does today, its known gaps, and how to report a problem.

JobRadar has not been formally audited against WCAG. The commitments below are goals the project works toward, not a certification.

## Where you interact with JobRadar

| Surface | What it is | Accessibility depends on |
|---|---|---|
| Command line | `jobradar init`, `run`, `chats`, `stats`… | Your terminal and screen reader, plus JobRadar's output (below) |
| Notion | Your job inbox, views and pages | Notion's own accessibility, plus the content JobRadar writes |
| Email (from v0.4) | Alerts and twice-daily digests | Your email client, plus the messages JobRadar sends |
| Documentation | README, wiki, design docs | GitHub's rendering, plus how the docs are written |

## What JobRadar does today

**Command line**
- Every result is plain text. Colour is only ever extra, never the only signal: errors also say what went wrong in words, and `stats` writes "under / over the 5% target" next to its percentages.
- Error messages name the exact setting or file to fix (for example `scoring.hide_below: Input should be less than or equal to 100`), so you don't have to find it visually.
- Log lines are one event per line, in a fixed `time level source: message key=value` format that works well with screen readers and `grep`.
- No command needs a mouse, and none uses full-screen or animated terminal interfaces. The only interactive prompt is the one-time Telegram login (phone number and code), which is plain line input.

**Notion content**
- Job titles are written as readable text ("Summer Trainee @ Reliance Jio Infocomm Ltd") rather than the decorative Unicode "bold" letters and emoji many channels use, which screen readers often read badly or skip.
- Details are structured as real headings ("Eligibility", "Skills") and bulleted lists inside the JobRadar toggle, so they can be navigated by heading and list.
- Status, work mode and scam risk are always written as words (New, Hidden, Remote, High), never as colour alone. Notion's select colours are optional decoration.
- Links have descriptive text ("Seen in Freshershunt · 2026-10-08") rather than bare URLs where JobRadar controls the text.

**Documentation**
- Diagrams in the wiki and docs are always explained in the surrounding text. Nothing important is only in a picture.
- Tables have header rows; commands are in code blocks that can be copied as text.

## Known limitations

- **Original posts are copied as-is.** The "Original post" quote inside each Notion page keeps the channel's own formatting, including emoji and Unicode "fancy" letters. The extracted fields above it are plain text.
- **Mermaid diagrams** in the wiki and docs are images to most screen readers. The text around each diagram describes the same flow; tell us if one doesn't.
- **Notion views** (Board, filters) are set up by you in Notion, so their accessibility is down to Notion itself.
- **Coloured terminal output** follows your terminal's colours. If red error text is hard to read, set `NO_COLOR=1` (supported by the CLI library JobRadar uses) or adjust your terminal theme.
- **Email alerts** are not built yet. When they are (v0.4), each email will have a plain-text version alongside HTML, real headings, and descriptive link text.

## Reporting a problem

If something in JobRadar, its Notion output or its documentation is hard or impossible for you to use:

1. [Open an issue](https://github.com/ThePlator/JobRadar/issues/new/choose) using the **Bug report** or **Documentation** template, and mention "accessibility" in the title.
2. Tell us what you were trying to do, what assistive technology you use (for example NVDA, VoiceOver, Orca, screen magnification), and where it went wrong.

Accessibility issues are labelled `accessibility` and treated as bugs, not feature requests. If you'd rather not report publicly, email the maintainer at contact.sameer04@gmail.com.

## For contributors

When you change something people see or read:

- Never use colour, emoji or position as the only way to convey meaning; say it in words too.
- Keep CLI output plain and line-based; put the actionable fix in error messages.
- Write Notion content with real headings and lists, and avoid decorative Unicode in text JobRadar generates.
- Give every diagram a text explanation, and every image alt text.
- Use descriptive link text instead of "click here" or bare URLs.
