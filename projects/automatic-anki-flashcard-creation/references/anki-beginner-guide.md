# Beginner Anki Setup and Import Guide

This guide assumes you have never used Anki. The workflow uses Anki's built-in text importer and requires **no add-ons**.

## 1. Understand the five Anki concepts

- **Note**: one stored fact or vocabulary item.
- **Field**: one piece of information inside a note, such as a word or example.
- **Note type**: the field structure shared by a kind of note.
- **Card template**: the HTML and CSS that decide what appears on the front and back.
- **Deck**: the group in which generated cards are studied.

One imported TSV row becomes one note. A note type may generate one or more cards; this guide creates one forward card per note.[1][2]

## 2. Install Anki Desktop

Download the current Anki Desktop release from the official site:

- <https://apps.ankiweb.net/>

Use the installer for your operating system. Avoid similarly named third-party apps. The exact version and operating-system requirements change, so use the requirements shown on the official download page.[3]

Open Anki and create a profile when prompted. A profile is a separate local collection and set of preferences.

## 3. Create a deck

1. On Anki's main screen, select **Create Deck**.
2. Enter a name such as `Language Vocabulary`.
3. Confirm.

The deck controls where imported cards are placed; it does not control their fields or appearance.

## 4. Create the four-field note type

1. Open **Tools → Manage Note Types**.
2. Select **Add**.
3. Clone **Basic** and name the new type `Automatic Language Flashcards`.
4. Select the new type and choose **Fields…**.
5. Rename or create the fields in this exact order:

   1. `Front`
   2. `Back`
   3. `Notes`
   4. `Example`

6. Delete unused fields only after confirming the four fields above remain.
7. Save and close the field editor.

Anki uses the first field when it checks for matching notes during text import, so `Front` should contain the stable vocabulary item rather than a changing explanation.[2][4]

If you choose different field names, change `field_mapping` in your skill configuration **and render matching templates**. From the installed skill directory, run:

```text
python <skill-directory>/scripts/render_anki_templates.py --config <config-path> --output-directory <safe-output-folder>
```

Copy the generated `front.html`, `back.html`, and `styling.css` into Anki instead of the default template files. Changing only `field_mapping` is not enough because Anki template field references are literal and case-sensitive.

## 5. Install the card formatting

With `Automatic Language Flashcards` selected in **Manage Note Types**, choose **Cards…**.

### Front Template

Replace the Front Template with the contents of:

```text
templates/anki/front.html
```

### Back Template

Replace the Back Template with:

```text
templates/anki/back.html
```

### Styling

Replace the Styling content with:

```text
templates/anki/styling.css
```

Use Anki's preview to confirm:

- The vocabulary item is prominent on the front.
- The answer appears after revealing the card.
- Empty Notes or Example fields do not leave empty sections.
- Dark mode remains readable.

Card templates use HTML and styling uses CSS. Field references such as `{{Front}}` are case-sensitive. `<br>` creates an in-field line break.[5][6][7]

## 6. Receive an export from the agent

A normal export contains two files:

```text
Alpha.tsv
Alpha.validation.json
```

Read the Validation Report first. It identifies generated, enriched, uncertain, duplicate, or rejected material. If the report marks an item for review, check it before importing.

Keep both files together until the batch is accepted.

## 7. Import the TSV

1. Open Anki Desktop.
2. Select **File → Import**.
3. Choose the received `.tsv` file.
4. Select note type **Automatic Language Flashcards**.
5. Select the intended deck.
6. Set the field separator to **Tab** if Anki does not detect it automatically.
7. Map the columns in order:

   | TSV column | Card Role | Anki field |
   |---|---|---|
   | 1 | Prompt | Front |
   | 2 | Answer | Back |
   | 3 | Notes | Notes |
   | 4 | Example | Example |

8. Enable **Allow HTML in fields** so `<br>` displays as a line break rather than visible text.
9. Review Anki's preview before importing.
10. Choose the duplicate behavior intentionally:
    - **Update** matching notes when this batch is meant to revise existing cards.
    - **Ignore** duplicates when existing cards must remain untouched.
    - **Import as new** only when duplicate first fields are intentional.
11. Select **Import**.

The files are UTF-8 plain text, one note per physical line, with literal tabs separating fields. Anki's built-in importer supports this directly.[8][9]

## 8. Inspect the result

Open **Browse** and inspect the imported notes:

- Front contains the intended word or phrase.
- Back contains the concise meaning.
- Notes contains the expected reading, pronunciation, or grammar information.
- Example displays two lines rather than visible `<br>` text.
- The cards belong to the intended deck and note type.

If the columns are wrong, delete or undo the import before studying, correct the field mapping, and import again.

## 9. Sync to AnkiWeb and mobile

AnkiWeb is the synchronization service. The TSV is imported into Anki Desktop first, and the resulting collection is then synchronized.[10]

1. Create an account at <https://ankiweb.net/> if you want cross-device sync.
2. In Anki Desktop, select the sync icon and sign in.
3. On the first sync, choose **Upload** when the Desktop collection contains the authoritative cards and AnkiWeb is empty.
4. Wait for synchronization to finish.
5. Install the official mobile client from the official apps page.
6. Sign in and synchronize the mobile client.
7. If the empty mobile client asks which side wins, choose **Download** from AnkiWeb.

The first sync can be one-way. If both sides already contain different cards, make a backup and read Anki's conflict guidance instead of guessing.[10][11]

## 10. Troubleshooting

### `<br>` appears as text

Re-import with **Allow HTML in fields** enabled.[9]

### Fields appear in the wrong places

The TSV columns were mapped incorrectly. Use the table in step 7.

### Some rows have missing or extra fields

Do not import the file. Run the included validator or ask the agent to regenerate it. Every row must have exactly four fields.

### Anki says a note already exists

Anki normally compares the first field and note type. Review the duplicate setting instead of clicking through automatically.[9]

### The card's appearance is wrong

Edit **Cards…**, not the font controls in **Fields…**. Review appearance is controlled by the card templates and CSS.[4][7]

### Sync asks Upload or Download

Stop and identify which collection is authoritative. The wrong first-sync choice can replace the other side.[10]

## 11. Optional add-ons

No add-on is required for this workflow. If a Language Profile recommends an optional add-on:

1. Confirm it supports your Anki version.
2. Open **Tools → Add-ons → Get Add-ons**.
3. Obtain the add-on code from AnkiWeb's shared add-ons directory.
4. Paste the code and restart Anki when instructed.
5. To remove it, open **Tools → Add-ons**, select it, and choose **Delete**.

Add-ons can modify Anki and can break after upgrades. Install only add-ons you understand and can remove.[12]

## Sources

[1]: https://docs.ankiweb.net/manual/getting-started
[2]: https://docs.ankiweb.net/manual/editing
[3]: https://apps.ankiweb.net/
[4]: https://docs.ankiweb.net/manual/editing#customizing-fields
[5]: https://docs.ankiweb.net/manual/templates/intro
[6]: https://docs.ankiweb.net/manual/templates/fields
[7]: https://docs.ankiweb.net/manual/templates/styling
[8]: https://docs.ankiweb.net/manual/importing/intro
[9]: https://docs.ankiweb.net/manual/importing/text-files
[10]: https://docs.ankiweb.net/manual/syncing
[11]: https://docs.ankiweb.net/ankimobile/syncing
[12]: https://docs.ankiweb.net/manual/addons
