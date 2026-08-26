# EV Fleet Cost Assistant

A real **chatbot** (`st.chat_message`/`st.chat_input`) for SGCarmart EV
market survey / fleet cost analysis, driving the same guided 3-step
workflow through free text:

1. **Refresh data** — reply "yes"/"no" to re-scrape SGCarmart EV listings
2. **Look up vehicle(s)** — chat in a brand/model name *or* a spec-based
   request ("SUV with at least 7 seats", "sedans that do 0-100 in under
   8 seconds") — matched against the local combined CSV
3. **Generate cost workbook** — reply "yes", fill in a short form for the
   numbers, get a 3-tab Excel file (Summary, Capital Cost, Recurrent Cost),
   live-formula-linked across tabs

The control flow and matching logic are deterministic (regex + pandas,
`utils/csv_tools.py`) — no LLM/API key is needed for the chatbot to
understand these requests. Step 3's maintenance costs and tab names stay
as structured form fields rather than free text on purpose: those values
feed straight into cost formulas, and a misheard number there is a silent
data error rather than a UX inconvenience. See "Optional: LLM-assisted
input" below if you want fully free-text Step 3 input added later.

The mascot (`assets/mascot.jpg`) is used as the page icon, header image,
and chat avatar throughout.

## Project layout

```
app.py                  # Streamlit chatbot entry point — chat history + step router
assets/
  mascot.jpg              # page icon, header image, and assistant chat avatar
utils/
  config.py              # paths + secrets/env handling
  pipeline_runner.py      # Step 1 — runs the 5 scraping stages as subprocesses
  csv_tools.py            # Step 2 — name matching + natural-language spec filtering
  electricity.py          # Step 3 — live tariff fetch with safe fallback
  workbook_builder.py      # Step 3 — bridges the UI to cost_engine.combinedcode
  claude_assistant.py      # Step 2 agent — Claude tool-use loop against the CSV
  ui_theme.py              # design system — global CSS, hero header, stepper (presentational only)
.streamlit/
  config.toml              # app-wide dark theme (colors/font) — ships as-is, not a secret
cost_engine/
  combinedcode.py          # unchanged cost logic (Capital/Recurrent/Summary builders)
  electricity_tariff.py    # unchanged SP Group tariff scraper
pipeline/
  sgcarmart_url.py          # Selenium — collect listing URLs
  spec_to_pricing_csv_converter.py
  spec_url_scraper.py       # Selenium — scrape spec pages
  pricing_url_scraper.py    # Selenium — scrape pricing pages
  combine_ev_csv.py         # joins specs + pricing into one CSV
data/                    # sgcarmart_ev_combined.csv, COEBiddingResultsPrices.csv,
                          # Capital Cost.xlsx (template) ship here
outputs/                 # generated workbooks land here
logs/                    # pipeline_run.log
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate       # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env             # optional, only needed for opt-in features
streamlit run app.py
```

## Known platform constraint

**Step 1's live scraping cannot run on Streamlit Community Cloud** — those
stages (`sgcarmart_url.py`, `spec_url_scraper.py`, `pricing_url_scraper.py`)
launch a real Chrome session via Selenium, and Cloud has no browser/network
egress for that. The app detects this automatically
(`pipeline_runner.scraping_available()`); when unavailable, Step 1 shows a
file-uploader instead so you can run the pipeline locally and upload the
resulting `sgcarmart_ev_combined.csv`.

To deploy on Streamlit Cloud, set `SCRAPING_ENABLED = "false"` in
`.streamlit/secrets.toml` (see `.streamlit/secrets.toml.example`) so this is
explicit rather than discovered at runtime.

## Data accuracy notes (important)

Several real bugs in the filtering/answer logic and the workbook engine
itself were found and fixed during development — worth knowing about
since they affect how results should be read:

- **Prices excluding COE.** ~27% of rows in `sgcarmart_ev_combined.csv`
  list a price suffixed `(w/o COE)` — that figure excludes COE entirely,
  and COE routinely adds $100k+ in Singapore. `csv_tools.resolve_true_price()`
  now adds the row's own `COE` column back in whenever this applies, and
  every price shown in chat is this corrected, all-in figure — never the
  raw column. If a row's COE amount itself can't be parsed, the price is
  reported as unknown rather than showing a misleadingly low number.
- **Criteria not tracked in the CSV** (ABS, EBD/EDB, ADAS, "boot space
  without folding seats") are detected and named explicitly in the chat
  reply — never silently dropped or guessed at.
- **Missing/unlisted spec data** (e.g. `Acceleration == "unknown"` for 53
  rows) is now tracked separately from genuine non-matches. A vehicle
  excluded because its own data is missing is reported as "excluded,
  not confirmed as a non-match" rather than being indistinguishable from
  one that actually failed the threshold. Those specific excluded rows
  are also browsable on request — say **"show missing data"** (or refer
  to the exact count mentioned, e.g. "what about the 39 vehicles?") right
  after a filtered search that had gaps, and they're presented the same
  way as any other pick-one list (number / model name / **all**).
- Every result line shows the raw CSV value(s) the match was actually
  based on (price, seats, range, boot, dimensions, acceleration —
  whichever the query asked about), so a parsing mistake would be visible
  immediately instead of hidden behind a derived label.
- **The COE-price bug existed in the actual Excel output too, not just
  chat.** `CapitalCostBuilder` reads `Current price` directly via its own
  parser, which never accounted for the `(w/o COE)` suffix — so a
  generated workbook's capital-cost figure for any of those ~27% of
  vehicles had the current COE premium subtracted from a price that never
  included it, silently understating capital cost by the COE amount.
  `workbook_builder.build_workbook()` now patches every roster row's price
  to the COE-corrected all-in figure before it ever reaches
  `CapitalCostBuilder` — verified by checking the actual generated
  worksheet cell, not just the input.
- **POA vehicles no longer block generation entirely.** Previously a POA
  (price-on-application) vehicle in the roster made `CapitalCostBuilder`
  raise mid-generation with no way to recover. Step 3's form now detects
  this in advance (`workbook_builder.needs_manual_price()`) and asks for a
  price inline, with "Generate workbook" disabled until every ⚠️-flagged
  vehicle has one.
- **Vehicle-type selection order.** A query mentioning two types (e.g.
  "sedan or SUV") used to always pick whichever type happened to come
  first in an internal list, regardless of which the person actually said
  first. Now picks whichever appears earliest in the query text.
- **Boot-capacity ranges.** ~15% of Boot/Cargo Capacity values are a range
  like `"180 - 580 L"` (seats-up to seats-down). Filtering used to compare
  against the smaller number, which could silently exclude a vehicle whose
  larger (seats-folded) capacity actually satisfied the request. Now uses
  the larger value for filtering; the raw range is still shown in results
  either way.
- All filter logic (vehicle type, acceleration, price, seats, range, boot,
  height, and combinations of these) was independently re-verified against
  plain pandas ground truth computed separately from the app's own code.

## Data source rule

Steps 2 and 3 only ever read the local CSVs — they never fetch
sgcarmart.com. The only exception is Step 3's electricity rate, which
*does* fetch live from SP Group (with a manual-entry fallback if that
fetch fails, e.g. in a sandboxed environment).

## Critical invariant: display names must match across tabs

`workbook_builder.build_workbook()` sets each vehicle's `display_name`
once on its `RosterVehicle`, and `combinedcode.assemble_and_save()` reads
that same name into all three tabs (Summary, Capital Cost, Recurrent
Cost) — this is what keeps the Summary's live formulas correctly linked to
the matching Capital/Recurrent blocks. Don't introduce a second source of
the display name anywhere in the UI layer.

## Design system (`utils/ui_theme.py`)

A full visual redesign layered on top of the existing structure — no
search/filter/workbook/Claude logic changed, this is presentation only:

- **App-wide dark theme** via `.streamlit/config.toml` (violet accent,
  Inter font) — applied natively so buttons, inputs, and sliders all
  inherit it automatically, rather than fighting Streamlit's defaults with
  CSS alone.
- **Hero header** with the mascot as a circular avatar (base64-embedded so
  it can sit inside custom HTML) in a soft gradient banner.
- **Custom stepper component** (`render_stepper`) replacing the old plain
  3-column text indicator — animated circle states (done/active/upcoming)
  connected by a progress line.
- **Card-based layout** throughout: `st.container(border=True)` is used
  for every logical group (sidebar status, AI assistant settings, upload
  fallback, workbook settings, download/success state), restyled globally
  via one CSS rule so every bordered container gets consistent rounded
  corners, subtle background, and a hover highlight — changing the look
  everywhere at once rather than per-instance.
- **Micro-interactions**: button hover lift + glow, press feedback,
  visible focus rings for keyboard navigation, a subtle fade-in on new
  chat messages, an animated gradient fill on the progress bar.
- **Clearer states**: a dashed-border empty state before any vehicle is
  found, a status badge (● Active / ○ Off) for the Claude assistant, an
  explicit success card with the file name once a workbook is generated,
  disabled-button + inline-error handling for the POA-price case (see
  above) instead of a dead-end failure.
- **Responsive**: Streamlit's column system already reflows on narrow
  viewports; a small media query hides the stepper's text labels (keeping
  just the numbered circles) below 640px so it doesn't wrap awkwardly on
  phones.
- **Accessibility**: color is never the only signal (⚠️/✓/✕ icons pair
  with every color-coded state), focus-visible outlines are added rather
  than removed, and the theme's contrast ratios were chosen for a dark
  background rather than relying on Streamlit's default light theme.

Verified with Streamlit's official `AppTest` framework (not just a
visual/manual check) across four real session states — initial load, Step
2 with a resolved vehicle, Step 3's form with a POA vehicle (exercising
the manual-price path), and the download/success state — confirming zero
exceptions in each. I don't have a browser in this environment to confirm
the final pixel-level look, so a manual pass to check spacing/contrast in
an actual browser is still worthwhile before relying on this.

## Removing a vehicle from the roster

Useful when a vehicle's CSV data turns out to be incomplete (e.g. an
unparsable COE category) and generation errors out on it. Three ways to
remove one, all equivalent:

- **Sidebar**: expand the roster list and click ✕ next to any vehicle.
- **Step 3 form**: each vehicle's card has a 🗑️ Remove button — the most
  useful spot, since that's exactly where a bad-data error surfaces. If
  the roster becomes empty, Step 3 shows a guard with a button back to
  Step 2 instead of letting you hit "Generate" with nothing to build.
- **Chat**: type `show roster` to see numbered entries, then
  `remove 2` or `remove BYD Seal` (by number or name substring — ambiguous
  matches are listed back for you to pick a number). Works identically
  whether or not a Claude API key is set — the key-present path exposes
  the same action as a `remove_from_roster` tool the agent can call on its
  own when you ask it to drop a vehicle.

## UI clarity

- A persistent 3-step progress indicator (`render_progress()`) always
  shows which step you're on and which are done — not just implied by
  chat history.
- A real "Continue to Cost Workbook →" button (`render_continue_button()`)
  appears in Step 2 as soon as at least one vehicle is in the roster —
  you're never solely dependent on typing "done" or waiting for Claude to
  decide to move on.
- Step 3's "Generate workbook" button is disabled (with an inline error)
  until every price-missing vehicle has a manual price entered, rather
  than letting you click it and hit a failure.

## Claude as the Step 2 agent (`utils/claude_assistant.py`)

Enter an Anthropic API key in the sidebar to turn this on — it's entered
per-session (never written to disk) and falls back to `ANTHROPIC_API_KEY`
in `.env`/secrets if you'd rather configure it server-side.

With a key set, **Claude answers all of Step 2's questions directly** —
this is now the primary path, not a fallback. It's a real tool-use agent
(`run_step2_agent`): Claude decides which tool(s) to call, Python executes
them against the actual CSV, results go back to Claude, repeat until it
has an answer. Available tools: `search_vehicles`, `filter_vehicles`,
`get_vehicle_fields`, `list_all_models`, `show_missing_data_vehicles`,
`add_to_roster`, `get_roster`, `transition_to_step3`.

This is kept strictly grounded so it can't reintroduce the
false-information bugs fixed earlier:

- Claude is explicitly told it must never answer a factual question about
  a vehicle's price/spec/feature from its own knowledge, even if it
  recognises the model — every number or name it states must have come
  back from a tool call in that conversation.
- `get_vehicle_fields` and `filter_vehicles` always return the
  COE-corrected all-in price (see the data-accuracy notes above) — Claude
  never sees the raw, potentially misleading `Current price` column
  directly.
- The system prompt tells Claude exactly which features this dataset does
  NOT track (ABS, EBD/EDB, ADAS, airbags, ISOFIX, seats-up-only boot
  capacity) so it reports that plainly instead of guessing.
- `filter_vehicles` reports the missing-data-excluded count the same way
  the deterministic path does, and Claude is instructed to mention it and
  offer `show_missing_data_vehicles` rather than silently dropping it.
- Side-effecting tools (`add_to_roster`, `transition_to_step3`) apply
  directly to session state — `transition_to_step3` refuses if the roster
  is empty, and the deterministic Step 3 entry question is still appended
  by Python afterward rather than left to Claude to phrase.
- Only the clean final text of each turn is kept in
  `session_state['agent_messages']` for next-turn context (capped to the
  last ~10 exchanges) — intermediate tool-call traffic isn't persisted, so
  history can't grow unbounded or end up with orphaned tool blocks.
- Any API error (bad key, network issue, timeout) returns a plain-text
  message rather than crashing the chat.

**Without a key**, Step 2 falls back to `handle_step2_deterministic()` —
the original regex/pandas-only path — so the app still fully works
offline; it just won't handle very open-ended phrasing or multi-step
follow-ups as gracefully.

Step 1 (subprocess side effects — scraping) and Step 3 (financial numeric
inputs, tab-name validation) deliberately stay **outside** the agent's
tool set and keep their existing structured/deterministic flows. Those
values feed straight into cost formulas or launch real subprocesses, and
an LLM mishearing "11.9k" as something else, or improvising around a
scraping failure, is a materially worse failure mode than a Claude answer
being imprecise. If you want to extend Claude to Step 3 too, follow the
same pattern as `filter_vehicles`: a forced tool call with a strict
schema, then validate the output the same way the structured widgets
already do (`workbook_builder.validate_tab_names`, numeric range checks)
before it ever reaches `build_workbook()`.

## Testing checklist

- [ ] Step 1: existing CSV present, "No" skips straight to Step 2
- [ ] Step 1: no CSV present at all — "No" is blocked, re-offers Step 1
- [ ] Step 1: on a scraping-capable host, a forced stage failure halts the
      pipeline and does not overwrite the existing CSV
- [ ] Step 1: on Streamlit Cloud (or `SCRAPING_ENABLED=false`), the
      uploader path replaces the CSV correctly
- [ ] Step 2: unique keyword auto-resolves
- [ ] Step 2: ambiguous keyword shows a picker; selection resolves correctly
- [ ] Step 2: unmatched keyword shows "no matches" + "show all models"
- [ ] Step 2: asking for a column not in the CSV (e.g. ABS) is reported as
      not available, not guessed
- [ ] Step 3: Marked vehicle zeroes COE/road tax in the output
- [ ] Step 3: Unmarked vehicle pulls the correct COE category premium
- [ ] Step 3: duplicate/blank/>31-char/invalid-character tab names are
      rejected with a clear message, not a traceback
- [ ] Step 3: display name is identical across all three tabs in the output
- [ ] Step 3: live electricity fetch failure falls back to manual entry
      without crashing
- [ ] Step 3: multi-vehicle roster produces one Summary row + one block per
      vehicle in Capital and Recurrent, in the same order
- [ ] Generated `.xlsx` opens cleanly and Summary formulas resolve after
      opening in Excel (or after running the xlsx skill's `recalc.py`)
