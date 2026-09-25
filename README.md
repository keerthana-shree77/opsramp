# OpsRamp Firmware Compliance & Comparison Portal

A Flask application that compares the firmware/software versions **actually
discovered in OpsRamp** (X-axis) against the **approved versions supplied in an
uploaded recipe file** (Y-axis), and reports the result for every asset and
component with a human-readable reason.

The application never fabricates a version or a recipe match. When OpsRamp does
not expose a required value it reports `VERSION NOT DETECTED` and preserves the
raw attributes it examined so the gap can be diagnosed.

---

## 1. Architecture and data flow

```
LOGIN → SELECT TENANT → SELECT CATEGORY → UPLOAD RECIPE
                                              │
                                    validate recipe (blocking)
                                              │
                          ┌───────────────────┴──────────────────┐
                          │  background job (progress polled)    │
                          │                                      │
                          │  search resources (paginated)        │
                          │  identify platform + category        │
                          │  deep-fetch only the matching assets │
                          │  flatten every attribute             │
                          │  score attributes per component      │
                          │  extract + normalize versions        │
                          │  match asset → recipe (L1/L2/L3)     │
                          │  compare versions / builds           │
                          └───────────────────┬──────────────────┘
                                              │
                            DASHBOARD  →  CSV / XLSX / JSON export
```

Three ideas carry the whole design:

**1. Nothing is matched by a single attribute name.** Each resource document is
flattened into `(path, key, value)` triples — including `attributes`,
`customAttributes`, nested hardware blocks and anything the detail endpoints
returned. Every triple is then *scored* against the alias list of the component
being looked for, with disqualifying words (`ip`, `date`, `serial`, `license`,
…) rejecting false positives such as `iLO IP Address` or `BIOS Date`. The
highest-scoring candidate wins; every scored candidate is preserved as raw audit
data.

**2. Which components to look for depends on the identified platform.** A
Superdome Flex is only interrogated for RMC, a DL server for BIOS/iLO/SPS/IP, a
switch for switch firmware, and so on. This is what keeps false positives low
and the OpsRamp call count proportional to the interesting assets.

**3. Broad matching is opt-in.** A recipe row for `DL380 Gen11` never silently
applies to every Gen11 server. Matching is tried at three levels — exact,
normalized (recipe model tokens must be a subset of the asset's), then family
(only when the recipe row uses a wildcard model or sets `Applies To Family`).
If two equally specific rows disagree, the result is `UNABLE TO COMPARE`, not a
guess.

### Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | Environment-driven settings, logging with credential redaction |
| `opsramp/auth.py` | OAuth2 client-credentials token cache (thread-safe, never logged) |
| `opsramp/client.py` | Pooled session, retries/backoff, timeouts, status→exception mapping, pagination |
| `opsramp/tenants.py` | Client/SAP tenant discovery + name filter |
| `opsramp/inventory.py` | Resource search and bounded-concurrency detail fetch |
| `firmware/normalizer.py` | Category/platform/model/component canonicalisation and alias tables |
| `firmware/extractor.py` | Flattening, platform identification, attribute scoring, version extraction |
| `firmware/comparator.py` | Version parsing and comparison |
| `firmware/matcher.py` | Deterministic asset→recipe matching (L1/L2/L3) |
| `firmware/models.py` | Normalized schemas and result statuses |
| `recipe/loader.py` | CSV/XLSX/JSON reading, header detection, column alias mapping |
| `recipe/validator.py` | Blocking validation with row-level errors |
| `recipe/normalizer.py` | Raw rows → canonical `RecipeEntry` |
| `service.py` | The end-to-end pipeline (pure data in, data out) |
| `jobs.py` | In-process job store and progress tracking |
| `store.py` | SQLite cache behind the matrix (measurements, recipe rows) |
| `recipes.py` | The library of approved recipes, on disk and in the cache |
| `overview.py` | The background sweep that fills and refreshes the matrix |
| `firmware/serialise.py` | Storing a tenant reading, and fingerprinting it |
| `recipe/merge.py` | Reading one baseline that arrives as several files |
| `reports/` | Summary cards, CSV, multi-sheet XLSX, JSON |
| `app.py` | Routes, auth, CSRF, upload handling, error pages |

---

## 2. Project structure

```
opsramp_firmware_tool/
├── app.py                      Flask routes and web security
├── config.py                   Environment configuration + logging
├── service.py                  End-to-end comparison pipeline
├── jobs.py                     Background job store / progress
├── overview.py                 The compliance-matrix sweep
├── recipes.py                  Library of approved recipes
├── store.py                    SQLite cache for the matrix
│
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── Dockerfile                  One process, one replica - see the note in it
├── compose.yaml
├── .dockerignore
├── README.md
│
├── opsramp/
│   ├── __init__.py
│   ├── auth.py
│   ├── client.py
│   ├── tenants.py
│   └── inventory.py
│
├── extension/                  Chrome extension: a button that opens the portal
│   ├── manifest.json
│   ├── background.js
│   ├── portal.js
│   ├── options.html / .css / .js
│   ├── managed_schema.json     What an administrator may set by policy
│   └── README.md               Installing, distributing, updating, removing
│
├── firmware/
│   ├── __init__.py
│   ├── models.py
│   ├── normalizer.py
│   ├── extractor.py
│   ├── matcher.py
│   └── comparator.py
│
├── recipe/
│   ├── __init__.py
│   ├── loader.py
│   ├── validator.py
│   └── normalizer.py
│
├── reports/
│   ├── __init__.py
│   ├── summary.py
│   ├── csv_report.py
│   ├── excel_report.py
│   └── json_report.py
│
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── index.html
│   ├── recipe_validation.html
│   ├── processing.html
│   ├── results.html
│   └── error.html
│
├── static/
│   ├── css/app.css
│   ├── js/{index,processing,results}.js
│   └── recipe_template.csv
│
├── tools/
│   └── make_recipe_xlsx.py     Turns the CSV template into an XLSX template
│
└── tests/
    ├── conftest.py
    ├── test_versions.py
    ├── test_recipe.py
    ├── test_matching.py
    └── test_extraction.py
```

---

## 3. Installation

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
```

Copy the configuration template and fill it in:

```bash
copy .env.example .env
```

Generate a strong secret key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Run it:

```bash
python app.py
```

The portal listens on `http://127.0.0.1:5000`. For local development over plain
HTTP set `SESSION_COOKIE_SECURE=false` and `FLASK_DEBUG=1`; never do either in
production.

---

## 4. Configuration

All configuration is environment-driven; there are no credentials in the source.

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Flask session signing key. Required. |
| `APP_USERNAME` / `APP_PASSWORD` | Portal sign-in credentials |
| `OPSRAMP_BASE_URL` | e.g. `https://your-instance.api.opsramp.com` |
| `OPSRAMP_PARTNER_TENANT_ID` | Partner tenant whose clients are listed |
| `OPSRAMP_OAUTH_CLIENT_ID` / `_SECRET` | Integration credentials |
| `TENANT_NAME_FILTER` | Substring/regex; only matching tenants are offered (`SAP`) |
| `MAX_WORKERS` | Concurrent detail fetches. Default 12, capped at 64. |
| `DEEP_FETCH_UNMATCHED` | Deep-fetch assets whose platform the summary did not reveal |
| `VERSION_COMPARE_POLICY` | `at_least` (newer is compliant) or `exact` |
| `HTTP_RETRY_TOTAL`, `HTTP_RETRY_BACKOFF` | Retry behaviour for 429/5xx |
| `OPSRAMP_PAGE_SIZE`, `OPSRAMP_MAX_PAGES` | Pagination limits |
| `MAX_UPLOAD_BYTES` | Upload size cap (default 10 MiB) |

See `.env.example` for the full list with comments.

### The compliance matrix

The home page is a matrix of account against category, populated without
anyone choosing a tenant or uploading a file each time. Three things make
that work.

**A library of recipes, set on the page.** An estate rarely runs one
baseline, so the portal holds several under names the operator chooses -
"March baseline", "January baseline", a stream pinned for one customer. Each
file is validated before it is accepted, stored under `var/recipes/`, and
survives a restart. Uploading this month's revision under a name already in
the library replaces that one, which is the ordinary monthly case. There is
nothing to edit and nothing to restart.

`DEFAULT_RECIPE_PATH` remains as a way to seed a fresh deployment: it stands
in as a single recipe while the library is empty, and the first upload takes
over from it.

**One read, many recipes.** Reading an account costs hundreds of OpsRamp
calls; measuring what was read against a recipe costs only CPU. `service.py`
keeps the two apart - `extract_tenant` then `compare_extraction` - so each
account is read once per sweep however many recipes it is measured against.
Adding a second recipe does not double the load on OpsRamp.

That is what makes attribution possible. Each cell carries a line per
recipe, and the account is labelled with the one it actually follows. That
is decided by **coverage** - how much of what is installed the recipe has an
approved target for - and not by the compliance rate, because a recipe
holding one target that happens to match would otherwise score 100% while
describing almost nothing.

**Each account is judged by the recipes you choose.** The **Recipe** column
holds a menu of checkboxes per row, and each account's choice is its own -
stored in `recipe_choices`, one row per account per recipe, so it survives a
restart. Tick none and the row shows every recipe with the closest match
marked, which is the attribution above. Tick one and the row shows that
recipe alone: the Overall cell, every category cell, the pending count and
the links all answer for it, and cells are re-scored from that recipe by
itself, so a cell another recipe could score does not look scored under this
one.

Tick several and **each keeps its own line, its own percentage and its own
counts** - which is how you read an account against the baseline it is on and
the one it is moving to at the same time. There is no blended figure, because
a blend would answer neither question. A line names its recipe whenever more
than one is in play, including when several are ticked but only one has
figures yet, so a lone percentage never leaves you guessing which baseline it
answers for; the ones still being checked are named beside the menu. The
pending count and the detail links follow whichever of the ticked recipes the
account's firmware follows most closely, so a reader always lands on figures
the row is actually showing.

One recipe in the set that is not in the library refuses the whole choice
rather than storing the half that is valid, which would be a choice nobody
made. A recipe that later leaves the library is taken out of every set that
named it, and an account left with none falls back to automatic.

Choosing re-compares nothing. Every account is measured against every recipe
in the library, so the figures for the one just chosen are already worked out
and stored - which is what makes the choice instant. What it does do is put
that account at the **front of the queue**: if a sweep is running, the account
is moved to the front of what is left instead of waiting behind two dozen
others nobody is looking at; if nothing is running, that one account is read
and measured on its own. Either way its stored figures stay on screen
meanwhile, and they are rewritten only if the reading's fingerprint has
actually moved - so choosing a recipe for an account whose firmware has not
changed costs one read and no writes.

A choice naming a recipe that has since left the library is no choice at all:
the row falls back to Automatic rather than rendering empty, and removing a
recipe releases the accounts pinned to it and says how many. An account
OpsRamp stops reporting takes its choice with it. Pinning an account to a
recipe it has no figures for yet - one uploaded moments ago - says so in the
row rather than leaving it blank, and the check that fills it in is already
running.

The estate-wide figures above the grid, and the per-recipe totals in the
recipe library table, stay per-recipe whatever any account is pinned to:
averaging across a mixture of baselines would not mean anything.

**Recipes for all accounts** sits beside the matrix heading and makes the same
choice once, for every account, overwriting whatever each was set to
individually. It reads *Mixed* while they differ rather than showing one
account's choice as though it spoke for the estate, and that reading
disappears once they agree. It re-compares nothing either, with one
exception: a recipe no account has figures for yet - one uploaded moments ago
- has the stored readings measured against it rather than the estate being
swept, which costs seconds and no calls to OpsRamp.

### What is never counted as an asset

A read keeps equipment and leaves out everything else, and getting that line
wrong is expensive in both directions. Left in, a part is counted as an asset
whose firmware could not be read: it drags its category's percentage down and
gives a reader nothing to act on. Left out, a real device goes missing from a
compliance report without anyone noticing.

Across this estate of 24 accounts, 4,818 resources are listed and 1,366 are
kept. What is dropped is checked by name, model and type rather than assumed:
DIMMs, fans, temperature sensors, backplanes, drives, vmnics, volumes,
datastores, containers, synthetic monitors, virtual machines, chassis
sub-records, and IP-only stubs that OpsRamp created from an alert and holds no
inventory for at all.

Three of those rules were worth 392 rows on their own:

* **A LUN is not an array.** An array presents hundreds of them and is its own
  resource; 360 `STORAGE_ARRAY_LUN`s were being counted as arrays whose OS
  version could not be read.
* **A guest operating system is not a hypervisor.** 31 NSX edge nodes - Ubuntu
  VMs with a model of `Other` and a tag reading `vmware_pool=VCF-edge...` -
  were filed as ESXi hosts on the strength of that tag, then reported as hosts
  whose build could not be read. The rule needs both a guest OS *and* nothing
  identifying the hardware, so a ProLiant running Red Hat is untouched.
* **What OpsRamp says a resource is outranks what it is called.** A container
  named `pdu_metrics` was filed as a power strip. The types that are never
  hardware are settled before the name is consulted; the name still rescues
  the types that are sometimes a mis-typing, so a switch typed `port` is a
  switch.

Together these took VERSION NOT DETECTED from 431 rows to 39 with every other
verdict byte-identical - UPDATED, NEEDS UPDATE, NOT FOUND IN RECIPE and
UNABLE TO COMPARE all unchanged, and the compliance rate with them. They were
never comparable; they were noise.

What remains unread is small and is not the tool's to fix: six SAN arrays that
OpsRamp holds no tags and no version for, seven Enlogic PDUs, and a handful of
entries whose model field contains a firmware version.

**Reading and measuring are separate.**  Reading an account costs hundreds
of API calls; measuring what was read against a recipe costs only CPU. They
are stored separately too - `inventory` holds what OpsRamp reported,
`measurements` holds what that came to when compared - because a recipe
changes far more often than an estate does. So:

* **A new recipe never triggers a sweep, and never waits for one.** Adding,
  replacing, renaming or removing one re-measures every account from the
  stored readings: a fraction of a second of work and no API calls, and
  removing a recipe takes its cells with it in the same pass. If a sweep is
  running - with a refresh every few minutes, likely - the accounts it has not
  reached yet pick the new recipe up immediately, and the ones already
  finished are caught by a re-measure the moment the sweep ends. Nobody has to
  press Refresh.

  Two caches make that cheap. Parsed recipes are held by a hash of their own
  bytes, so the same files are not read again on every sweep - and a revision
  is never served from the cache, which a size-and-timestamp key could get
  wrong. And the handful of string functions the matcher calls once per recipe
  entry per component are memoised. Together they took a re-measure of
  twenty-four accounts against three recipes from 3.4s to 0.14s, with every
  one of 6,804 verdicts - and every reason string - byte-identical.
* **The background scan is quiet.** Every ten minutes it re-reads the estate
  and fingerprints what it found. An account whose firmware has not moved,
  measured against recipes that have not moved, is left exactly as it was -
  no re-measuring, no churn under a reader's cursor. Only what changed is
  written.
* **Signing back in costs nothing.** The grid renders from the cache, with
  the same figures against the same recipes as when the session ended.

A run - the rows behind one cell - lives in memory, so after a restart a
cached cell would link to nothing. The `runs` table records which account
and recipe each one belonged to, and the run is simply built again from the
stored reading when someone clicks through. A cached figure a reader cannot
open is half a feature.

Readings are stored as compressed JSON, so anyone can decompress one and
read what the tool believed was installed - which matters for a tool whose
whole job is to be checked.

```
OVERVIEW_CATEGORIES=all
OVERVIEW_REFRESH_MINUTES=10     # 0 leaves it to the Refresh button
OVERVIEW_REFRESH_ON_START=true
OVERVIEW_MAX_TENANTS=0          # 0 means no limit
OVERVIEW_TENANT_WORKERS=4       # accounts read at once; 1 is one at a time
CACHE_DB_PATH=var/cache.db
```

**Accounts are read several at a time.** Reading is dominated by waiting on
OpsRamp, so `OVERVIEW_TENANT_WORKERS` accounts are read in parallel, each with
its own connection pool - sharing one would serialise them on it. Each account
already fans out over its own assets, so the requests in flight are roughly
this times `MAX_WORKERS`; the ceiling is what the API tolerates. Re-measuring
from stored readings stays sequential, being CPU-bound. On a stubbed estate of
twelve accounts, six at a time ran 3.9x faster and produced identical figures.

An account being re-measured keeps showing its last result rather than
blanking, so a sweep does not empty most of the grid while it runs.

A sweep over a large estate can take longer than the refresh interval. A
tick that arrives while a sweep is still running is dropped rather than
starting a second one, so sweeps run back to back - which also means the
load on OpsRamp is continuous at ten minutes. Raise the interval if that
matters more than how fresh the grid is; 720 is twice a day.

Accounts are swept one at a time and each one's cells are published as it
finishes, so the grid fills in progressively and a single failing account is
marked as failed rather than losing the sweep. Clicking a percentage opens
that recipe's rows for that account, with the category and NEEDS UPDATE
filters applied.

**Every cell says something.** A cell with no percentage is a finding, not a
gap in the rendering, so it never appears as a bare mark:

| Cell shows | Means |
| --- | --- |
| `45.0%` and `9/20` per recipe | the rate, and what it was computed from |
| `no assets` | the account has nothing in that category |
| `not scored` | it has assets, but nothing comparable; hovering says whether the recipe has no target or OpsRamp reported no version |
| `no target` on one line | that particular recipe says nothing about this equipment |
| `not measured against X yet` | the account is pinned to a recipe it has no figures for; the check is running |

Colours follow the percentage: **above 90%** green, **80 to 90%** amber,
**below 80%** red. A cell with no percentage is left uncoloured rather than
red - there is nothing to judge, and red would report a gap in the recipe as
a compliance failure. The bands are `TONE_GREEN_AT` and `TONE_AMBER_AT` in
`reports/summary.py`, and every colour in the tool comes from that one
function.

Until a recipe is added the page asks for one, and one-off comparisons from
the form still work.

### Model aliases

An alias may be limited to named accounts, because the same OpsRamp model can
stand for different hardware in different ones. In this estate "HP_3PAR" is
not a 3PAR at all - the label is wrong at the source - and it stands for two
different arrays, so one line cannot correct it:

```
OpsRamp Model,Recipe Model,Component,Tenant,Notes
HP_3PAR,Alletra MP,,grr01;grr02;idp01;idp02,mislabelled by OpsRamp
HP_3PAR,Alletra 9060,,,mislabelled by OpsRamp
```

An alias naming accounts beats one that names none, for those accounts, so the
estate-wide row does not have to list every account it does not cover. An
account is named by any part of its name, and the parts are separated by
semicolons so the cell needs no quoting. The two are never offered together:
that would make the match ambiguous and report neither.


OpsRamp reports a part number where the vendor recipe names a product line:
`G620` against "HPE StoreFabric SN6600B Fiber Channel Switch", `P9R53A`
against "HPE Metered and Switched PDU". Nothing in either source states that
these are the same device, so the tool does not infer it. Declare the
equivalence in `model_aliases.csv` at the project root:

```csv
OpsRamp Model,Recipe Model,Component,Tenant,Notes
G620,SN6600B,,,Brocade G620 = HPE StoreFabric SN6600B
P9R53A,Metered and Switched,,,HPE G2 Metered & Switched PDU
```

`Component` is optional. Leave it blank and the alias applies to every
component. Name one and the alias applies only to that component - which is
how a platform whose recipe section omits one component could borrow that
target from comparable hardware, without disturbing the targets its own
section does state. A scoped alias may point at a row under a different
platform; the category must still agree.

A component the recipe states no target for is more often one the operator
simply does not track, and then the answer is not to borrow a target but to
stop looking: a platform's component list in `firmware/normalizer.py` decides
what is read at all. The Alletra Storage Server is the case in point - its
section gives BIOS and SPS, and its iLO is neither read nor compared.

Columns are read by name from the header row, so `Notes` can sit anywhere and
a file without a `Component` column - or without a header at all - still
loads.

The left column is what OpsRamp reports; the right column is any text from the
recipe that identifies the same device - a model code such as `SN6600B` is
enough, it need not be the whole row. A bare token also works, so `8325` maps
anything whose model contains it. Lines starting with `#` are ignored, and the
file is read at startup.

Two properties make this safe to rely on:

* Every alias that takes effect is named in the report's **Reason** column and
  the match level reads `L2c model alias`, so a mapping can always be traced.
* A direct match always wins over an alias, and each alias is evaluated on its
  own - so if two aliases point at rows with different target versions, the
  result is `UNABLE TO COMPARE` naming both, never a silent pick.
* A shared model code is not sufficient on its own. `DL380` is common to
  DL380 Gen11 and DL380 Gen10, so one side's model tokens must contain the
  other's; borrowing a Gen10 target for a Gen11 machine cannot happen.

When no alias applies, the `NOT FOUND IN RECIPE` reason lists the models the
recipe *does* cover for that component, which is usually enough to write the
alias you need.

### Adapting the API endpoints

Every OpsRamp path is a template in `config.py` and can be overridden by
environment variable without touching code:

```
OPSRAMP_TOKEN_PATH=/auth/oauth/token
OPSRAMP_CLIENTS_PATH=/api/v2/tenants/{partner_id}/clients/search
OPSRAMP_CLIENTS_FALLBACK_PATHS=/api/v2/tenants/{partner_id}/clients/minimal,/api/v2/tenants/{partner_id}/clients
OPSRAMP_RESOURCE_SEARCH_PATH=/api/v2/tenants/{tenant_id}/resources/search
OPSRAMP_RESOURCE_DETAIL_PATH=/api/v2/tenants/{tenant_id}/resources/{resource_id}
OPSRAMP_RESOURCE_EXTRA_PATHS=            # comma-separated, optional
```

The client listing is the one endpoint that genuinely varies between OpsRamp
versions, so it is resolved at runtime: `OPSRAMP_CLIENTS_PATH` is tried first
and, **only** if the instance answers 404 or 405, each entry of
`OPSRAMP_CLIENTS_FALLBACK_PATHS` is tried in turn. The path that worked is
written to the log. Any other status (401, 429, 5xx) surfaces immediately
rather than being masked by a retry against a different path.

A bare `/clients` is the *create* endpoint and answers GET with **HTTP 405** —
that is the usual cause of "OpsRamp returned HTTP 405" on the home page.

`OPSRAMP_RESOURCE_EXTRA_PATHS` is the extension point for instance-specific
endpoints that carry firmware detail. Anything returned is merged into the
resource document under `_extra` and becomes visible to the extraction engine
automatically — no code change needed.

**Tune the extraction against your own data.** Run one comparison, download the
**Raw OpsRamp CSV**, and look at the `Raw Attribute Name` column. If a version
lives under a name the alias tables do not cover, add that spelling to
`firmware/normalizer.py` → `COMPONENTS[<component>]["aliases"]`. If something
irrelevant is being picked up, add a word to the same component's `exclude`
list. `tests/test_extraction.py` holds synthetic payloads in the shapes this
code expects — adjust them to match your instance and the tests become your
regression suite.

---

## 5. Recipe file

### Formats

The published vendor version matrices can be uploaded **as PDFs, exactly as
they come** - there is no need to convert them to Excel first. XLSX, CSV and
JSON are read too. A PDF is read with `pdfplumber`, page by page; a table
that runs over a page break keeps neither its header nor its section
heading, so both are carried forward.

**Several files can make one recipe.** The monthly baseline is published as
two documents - the CDC matrix and the S4HANA matrix - which describe one
set of approved versions between them. Select both on the upload form and
they are merged under one name. Held apart as two recipes, each would score
badly on the equipment the other covers and the coverage figures would mean
nothing. If any one file of a recipe is unreadable or invalid the whole
upload is refused: accepting the rest would quietly narrow the baseline.

### Which column is the target

These files are reissued monthly and the conversion is not stable between
issues. One carried the source page number in column A, shifting every other
column right by one. Another listed the previous release beside the current
one:

```
Components | Help Command | Recipe 2026.01 | Recipe 2026.02 | Links
```

so "the first value after the component name" stopped being the answer - it
was the help text, and failing that it was *last month's* version. The value
column is therefore chosen by its label, and the latest labelled release
wins. A column that is *about* the target rather than the target itself is
never chosen, however it is labelled: one issue calls its links column
`Recipe 2025.01 links`, which begins the same way as the version column and
carries the same release, and reading it made every target a URL. As a last
resort a value that is plainly a link is refused and the line reported. Where a release column is blank the row is reported as having no
target rather than falling back to the previous one: approving a superseded
version is worse than reporting none.

### The plain table layout
Download the starting point from the home page, or find it at
`static/recipe_template.csv`. Generate an XLSX version with:

```bash
python tools/make_recipe_xlsx.py
```

| Column | Required | Notes |
| --- | --- | --- |
| Platform | Yes | Superdome Flex, HPE DL, CSUS, Aruba, Storage, VMware, … |
| Model | No | Blank or `*` makes the row a platform-family fallback |
| Category | Yes | Server / Switch / Storage / ESXi |
| Component | Yes | RMC, BIOS, iLO, SPS, Intelligent Provisioning, Switch Firmware, OS Version, ESXi |
| Target Version | Yes | Or `N/A` when the component does not apply to that model |
| Target Build | No | Numeric; takes precedence over the version when supplied |
| Mandatory | No | Yes / No, defaults to Yes |
| Applies To Family | No | Yes lets the row match every model of the platform |
| Notes | No | Free text, carried into `Recipe_Data` |

Column names are matched by alias, so `Target`, `Latest Version` and
`Approved Version` all map to Target Version. Header rows can appear after a
title/preamble block. Validation runs **before** any OpsRamp call and reports
row numbers:

```
Recipe Validation Failed

3 errors detected:
1. Row 14: Component is missing.
2. Row 27: Target Version is missing.
3. Row 42: Duplicate entry for DL380 Gen11 / BIOS.
```

---

## 6. Results

| Status | Meaning |
| --- | --- |
| `UPDATED` | Installed version satisfies the approved target |
| `NEEDS UPDATE` | Installed version is behind the approved target |
| `VERSION NOT DETECTED` | Asset found, but no recognised attribute carried the version |
| `NOT FOUND IN RECIPE` | No recipe row covers this platform/model/component |
| `NOT APPLICABLE` | The recipe marks the component as N/A |
| `UNABLE TO COMPARE` | Values are not comparable (firmware-family mismatch, missing build, ambiguous recipe) |

Every row carries a reason, the source attribute the version came from, and the
match level (`L1 exact`, `L2 normalized`, `L3 family`) — so any verdict can be
traced end to end.

Exports:

* **Comparison CSV** — the normalized result table
* **Raw OpsRamp CSV** — every scored attribute, selected or not (X-axis audit)
* **Recipe CSV** — the normalized recipe actually used (Y-axis audit)
* **XLSX** — `Summary`, `Comparison`, `Installed_Data`, `Recipe_Data`, `Exceptions`
* **JSON** — machine-readable, for downstream tooling

### Version comparison

Versions are structurally parsed, never string-compared:

| Input | Parsed as |
| --- | --- |
| `1.2.3`, `v1.2.3` | release `(1,2,3)` |
| `U54 v2.10` | prefix `u54`, release `(2,10)` |
| `8.0 Update 3` | release `(8,0,3)` |
| `8.0.3 build-24022510` | release `(8,0,3)`, build `24022510` |

The verdict is three-way, and it is the same three rules everywhere:

| Installed against the approved target | Result |
| --- | --- |
| the same | UPDATED |
| older | NEEDS UPDATE |
| newer | UPDATED (with the reason saying so) |

`VERSION_COMPARE_POLICY=exact` narrows the third row to NEEDS UPDATE, for
an estate that must sit on the approved version and not past it.

A revision suffix is ordered rather than given up on: `9.2.2c` against
`9.2.2c1`, or ESXi `8.0 U3i` against `8.0 U3k`, are successive revisions of
one release and the same three rules apply to them. Digit runs inside a
suffix compare as numbers, so `c10` follows `c9`.

Where the recipe pins a **Target Build** and the inventory reports none, the
verdict is given on the versions and the reason records that the build could
not be checked. ESXi does this on every host - its build is published in the
version matrix and is rarely discoverable from OpsRamp - and refusing to
answer left every ESXi row unscored.

What is still refused: **different firmware-family prefixes**. `U54` against
`U32` is the DL380 Gen11 ROM against the Gen10 one, and they do not order.
A verdict there would not be conservative, it would be wrong - and it almost
always means a recipe row has been matched to the wrong model, which is
worth seeing. Those rows read UNABLE TO COMPARE.

---

## 7. Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

110 tests cover version parsing and comparison, component/platform/model
normalization, recipe loading and validation, the matching hierarchy (including
that over-broad matching does *not* happen), and extraction against synthetic
OpsRamp payloads for all six platform families.

With coverage:

```bash
python -m pytest --cov=firmware --cov=recipe --cov=opsramp --cov-report=term-missing
```

---

## 8. Deployment

**Run exactly one worker process.** Jobs and results live in this process's
memory, so a multi-process server would scatter them across workers. Use
threads for concurrency, not processes.

Linux (gunicorn):

```bash
gunicorn --workers 1 --threads 8 --timeout 120 --bind 127.0.0.1:8000 app:app
```

Windows (waitress):

```bash
waitress-serve --listen=127.0.0.1:8000 --threads=8 app:app
```

Put nginx/IIS in front for TLS termination:

```nginx
server {
    listen 443 ssl;
    server_name firmware.internal.example;

    ssl_certificate     /etc/ssl/certs/firmware.crt;
    ssl_certificate_key /etc/ssl/private/firmware.key;

    client_max_body_size 12m;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
```

### Behind a proxy

Terminating TLS upstream leaves the app on a plain HTTP connection, so it
believes the request arrived over HTTP: redirects come back as `http://`, and
every line in the log is the proxy's address rather than the caller's. The
real values are in the `X-Forwarded-*` headers, and `TRUSTED_PROXY_HOPS` says
how many proxies may be believed:

```bash
TRUSTED_PROXY_HOPS=1
```

Count them rather than guessing, and leave it at `0` when nothing is in front.
Each header is a list that anything upstream may append to: claim one hop with
two in front and the app reads a value the client sent itself, and claim a hop
that is not there and anything able to reach the app can assert its own
address and scheme. Getting it wrong in that direction is what turns a Secure
cookie into one that is never actually protected.

### As a container

`Dockerfile` and `compose.yaml` build and run it. Nothing is configured in the
image: the `.env` is read at run time, so the same image runs in test and in
production.

```bash
cp .env.example .env     # then fill it in
docker compose up -d --build
docker compose logs -f portal
```

It listens on `127.0.0.1:8000` for a reverse proxy to publish, holds the cache
and the uploaded recipes on the `portal-data` volume, runs as an unprivileged
user with a read-only root filesystem, and drops every capability.

**One replica.** Not a default to tune - a constraint. Jobs, their progress and
the compliance matrix live in the process's memory, and the sweep runs on a
background thread started when the module is imported. A second container is a
second sweep against the same OpsRamp rate limit, each answering from the half
of the results it happens to hold. Give it more threads, or a bigger machine,
never more copies.

Two paths are worth knowing about:

* `/data` is the only writable location, and the only thing worth backing up.
  Losing it costs a full re-sweep of every account, not any data you cannot
  rebuild.
* `DEFAULT_RECIPE_PATH` and `MODEL_ALIASES_PATH`, if you use them, name files
  that have to be mounted into the container to exist inside it.

Checklist:

* `SESSION_COOKIE_SECURE=true` and serve only over HTTPS.
* Supply the `.env` through your secret manager or the service unit's
  `EnvironmentFile`, readable only by the service account.
* Keep `MAX_WORKERS` well under the OpsRamp API rate limit; start at 12.
* Ship the application log to your log collector. It redacts bearer tokens,
  client secrets and passwords, but never enable `FLASK_DEBUG` in production.

### When OpsRamp says you are asking too fast

A sweep runs up to `MAX_WORKERS` requests per account across
`OVERVIEW_TENANT_WORKERS` accounts at once - 48 by default - so a rate limit
is not an unlucky single request: when one worker is refused the rest are
about to be. Each retrying on its own, into the same crowded few seconds,
they ran out together.

That mattered more than it sounds. A PDU publishes its firmware on the detail
endpoint and nowhere else, so a call lost to a 429 is the whole row, and it
was recorded as `VERSION NOT DETECTED` - which sends somebody to look at a
PDU that was fine while hiding the fact that nobody managed to ask.

So the pause is shared by every client in the process, because the limit
belongs to the account and not to the connection. The first refusal sets it
and everybody waits it out instead of adding to the pile; it honours
`Retry-After`, doubles while the limit persists and is capped at a minute.
The refused call is then tried once more from that quiet line, and anything
still refused at the end of the sweep is gone over one at a time - the pass
that failed was the crowded one. A resource that keeps refusing still says
so, rather than claiming the hardware has no firmware.

Lowering `MAX_WORKERS` remains the way to be a quieter neighbour; this is
what keeps the report honest when you are not.

### Scaling beyond one process

The job store is the only stateful component. To run multiple workers, replace
`jobs.py` with Redis/RQ or Celery and persist `ComparisonResult` — nothing in
`service.py`, `firmware/` or `recipe/` needs to change, since the pipeline is
pure data in, data out.


### Publishing this repository

The repository belongs on an internal host. It carries no credentials - `.env`
has never been committed - but it does carry operating knowledge of one
estate, and two things in it name that estate rather than the tool:

* **`model_aliases.csv`** names four accounts, because the alias it scopes to
  them is only correct for them. That is configuration, not documentation, and
  deleting the names would make the tool wrong. A site that would rather not
  commit them sets `MODEL_ALIASES_PATH` to a copy kept beside the `.env`, and
  the shipped file stays as the example.
* **The recipes and the cache** under `var/` and `UPLOAD_DIR` are ignored
  already, and should stay ignored: they hold the firmware inventory itself.

Test fixtures name no real host, address or serial. They use RFC 5737
documentation addresses and invented account codes, and are meant to keep
doing so - a fixture copied from a live payload should be rewritten before it
is committed.

### What cannot host this

**GitHub Pages cannot run this application,** on this instance or any other.
Pages serves static files. This is a Flask application: it holds an
authenticated session, sweeps the OpsRamp API on a background thread, and
keeps a SQLite cache. Nothing of that survives being reduced to static files,
and pushing the repository does not put the portal anywhere - it puts the
source somewhere.

It needs a host that runs a Python process: a VM or container with the
service under `gunicorn` or `waitress` as in section 8, one worker, behind a
reverse proxy that terminates TLS. Which internal platform that is, and how
deployment reaches it from the repository, is a decision for whoever runs
internal hosting.

---

## 9. Security notes

* No credentials in source; everything comes from the environment.
* OAuth tokens are held in memory only, never rendered, never logged — a
  logging filter redacts bearer tokens, `client_secret`, `access_token`,
  `password` and `Authorization` from every record.
* Sessions: HttpOnly, SameSite=Lax, Secure by default; the session (and the
  CSRF token) is rotated on login.
* CSRF tokens on every state-changing request.
* Uploads: extension allow-list, size cap, `secure_filename`, stored under a
  generated name in a directory outside `static/`, parsed as data only, and
  deleted immediately after parsing. Uploaded files are never executed.
* Jobs are scoped to the session that created them.
* All template output is auto-escaped; `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy` and a `Content-Security-Policy` that
  forbids inline scripts are set on every response.
* Users see actionable error messages; stack traces go to the server log only.

---

## 10. Known limitations

* **Endpoint assumptions.** The default paths follow the OpsRamp v2 API
  surface. Confirm them against your instance and override via environment
  variable if they differ. No endpoint is invented beyond these four.
* **Attribute names are instance-specific.** The alias tables cover the common
  spellings; use the Raw OpsRamp CSV to find the ones your instance uses and
  extend `firmware/normalizer.py` accordingly.
* **Single process.** See the deployment section. The cache is SQLite on the
  local disk, so a second process would keep its own copy of the matrix and
  sweep independently.
* **The grid is as fresh as the last sweep.** Percentages carry the timestamp
  of the sweep they came from, not of the moment you looked. On an estate
  where a sweep takes longer than `OVERVIEW_REFRESH_MINUTES`, the oldest row in
  the grid is roughly one sweep old.
* **Attribution is a reading, not a record.** The recipe an account "follows"
  is inferred from which one has approved targets for the most of what is
  installed. It is a good signal, but it is not a statement of what anyone
  intended - two recipes that overlap heavily can swap places between sweeps.
* **Legacy `.xls`** is not supported; re-save as `.xlsx` or `.csv`.
