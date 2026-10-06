"""Editable text fields in APPROVAL (a teacher correction, a drop reason), kept apart from immutable previews."""
BEGIN = "<!-- gtd-agent:feedback-begin -->"
END = "<!-- gtd-agent:feedback-end -->"
HEADING = "**Your correction:**"
PLACEHOLDER = "Write your correction here, then check Teacher feedback."
REASON_HEADING = "**Your reason:**"
OUTCOME_HEADING = "**What came out of it:**"
FIELDS = {HEADING: PLACEHOLDER, REASON_HEADING: "Write why you dropped it, then tick Save reason.",
          OUTCOME_HEADING: "Write one line, then tick Save."}


def field_heading(body: str) -> str | None:
    """The heading of the text field in a block, if it has one."""
    if BEGIN not in body:
        return None
    field = body.split(BEGIN, 1)[1].strip()
    return next((heading for heading in FIELDS if field.startswith(heading)), None)


def split_feedback(body: str) -> tuple[str, str]:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if BEGIN not in body and END not in body:
        return body.strip(), ""
    if body.count(BEGIN) != 1 or body.count(END) != 1:
        raise ValueError("Feedback field is incomplete; finish editing before submitting")
    before, rest = body.split(BEGIN)
    field, after = rest.split(END)
    heading = next((h for h in FIELDS if field.strip().startswith(h)), None)
    if after.strip() or "**Decision (choose one):**" not in before or heading is None:
        raise ValueError("Your text must stay inside its field")
    field = field.strip()[len(heading):].strip()
    lines = [line.lstrip()[1:].lstrip() if line.lstrip().startswith(">") else line for line in field.splitlines()]
    text = "\n".join(lines).strip()
    if text == FIELDS[heading]:
        text = ""
    if "<!-- gtd-agent:" in text:
        raise ValueError("Feedback cannot contain approval control markers")
    return before.strip(), text


def with_feedback(body: str, text: str = "", heading: str | None = None) -> str:
    heading = heading or field_heading(body) or HEADING
    base, _ = split_feedback(body)
    quoted = "\n".join("> " + line for line in (text or FIELDS[heading]).splitlines())
    return f"{base}\n\n{BEGIN}\n{heading}\n{quoted}\n{END}"
