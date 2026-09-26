# How to add a doc

1. Pick the section: tutorial, how-to, reference, or explanation.
2. Create a Markdown file in `docs/<section>/`. Use lowercase-with-dashes names.
3. Add it to the `nav` list in `mkdocs.yml`.
4. Preview locally: `pip install -r requirements.txt` then `mkdocs serve`.
5. Open a pull request. Use the PR checklist.
6. On merge, Netlify rebuilds the site.

## Writing rules

- Describe what the tool actually does today. Not what it might do.
- Every result names the task it was run on.
- Credit tools, models, and datasets, with licenses.
- No real client, employer, or student data. Use synthetic examples.
