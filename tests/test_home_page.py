"""What the compliance matrix actually renders.

The bug this guards against was a rendering one, not an arithmetic one: cells
that came out as a bare mark, which reads as a broken page rather than as the
finding it is. So these tests look at the HTML, not at the numbers behind it.
"""
import os
import re
import tempfile

import pytest

# The application reads its settings at import time, and ``load_dotenv`` does
# not override what is already set - so a developer's own .env cannot leak in
# and quietly change what these tests are measuring.
_SANDBOX = tempfile.mkdtemp(prefix="portal-home-")
os.environ.update(
    {
        "SECRET_KEY": "test-secret",
        "APP_USERNAME": "tester",
        "APP_PASSWORD": "hunter2",
        "OPSRAMP_BASE_URL": "https://opsramp.invalid",
        "OPSRAMP_PARTNER_TENANT_ID": "partner",
        "OPSRAMP_OAUTH_CLIENT_ID": "id",
        "OPSRAMP_OAUTH_CLIENT_SECRET": "secret",
        "DEFAULT_RECIPE_PATH": "",
        "SESSION_COOKIE_SECURE": "0",
        "OVERVIEW_REFRESH_ON_START": "0",
        "OVERVIEW_REFRESH_MINUTES": "0",
        "UPLOAD_DIR": os.path.join(_SANDBOX, "var", "uploads"),
        "CACHE_DB_PATH": os.path.join(_SANDBOX, "var", "cache.db"),
    }
)

import app as portal  # noqa: E402
from overview import TenantState  # noqa: E402
from recipes import RecipeRef  # noqa: E402


RECIPE_CSV = (
    "Platform,Model,Category,Component,Target Version\n"
    "HPE DL,DL380 Gen11,Server,BIOS,U54 v3.00\n"
)


def line(name, job_id, rate, updated=0, comparable=0, covered=0, components=0,
         not_detected=0, not_in_recipe=0, recipe_id=None):
    return {
        "id": recipe_id or name.lower(),
        "name": name,
        "job_id": job_id,
        "rate": rate,
        "tone": "ok" if (rate or 0) >= 95 else "bad",
        "updated": updated,
        "needs_update": comparable - updated,
        "not_detected": not_detected,
        "not_in_recipe": not_in_recipe,
        "comparable": comparable,
        "covered": covered,
        "coverage": round(100.0 * covered / components, 1) if components else None,
        "components": components,
        "pending": (comparable - updated) + not_detected,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    portal.app.config["TESTING"] = True
    portal.app.config["SESSION_COOKIE_SECURE"] = False

    # A library of two recipes, without touching the real one on disk.
    refs = [
        RecipeRef("cur", "Current", "march.xlsx", [tmp_path / "march.xlsx"],
                  targets=120),
        RecipeRef("leg", "Legacy", "jan.xlsx", [tmp_path / "jan.xlsx"], targets=90),
    ]
    monkeypatch.setattr(portal.recipe_library, "all", lambda: refs)
    monkeypatch.setattr(
        portal.recipe_library,
        "describe",
        lambda: {
            "configured": True,
            "count": len(refs),
            "seeded": False,
            "recipes": [r.public() for r in refs],
        },
    )

    # No account has been pinned to a recipe unless a test says so, and what a
    # test pins is forgotten again afterwards.
    monkeypatch.setattr(portal.overview_store, "_selections", {})

    with portal.app.test_client() as test_client:
        with test_client.session_transaction() as session:
            session["user"] = "tester"
            session["_csrf"] = "test-csrf"
        yield test_client


def publish(store, state: TenantState) -> None:
    store._tenants = {state.tenant_id: state}
    store._order = [state.tenant_id]
    store.status = "done"


def page(client) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


# ------------------------------------------------------------ no blank cells


def test_a_category_with_no_assets_says_so(client):
    publish(
        portal.overview_store,
        TenantState(
            tenant_id="t1", name="SAP-one", status="done",
            recipes=[line("Current", "j1", 50.0, 1, 2, 2, 2, recipe_id="cur")],
            best="cur",
            cells={
                "Server": {
                    "assets": 1, "components": 2, "best": "cur",
                    "comparable_any": True, "explain": "",
                    "recipes": [line("Current", "j1", 50.0, 1, 2, 2, 2,
                                     recipe_id="cur")],
                }
            },
        ),
    )
    html = page(client)
    assert "no assets" in html, (
        "an account with nothing in a category must say so, not show a mark"
    )


def test_a_category_with_nothing_comparable_says_why(client):
    publish(
        portal.overview_store,
        TenantState(
            tenant_id="t1", name="SAP-one", status="done",
            recipes=[line("Current", "j1", None, 0, 0, 0, 14,
                          recipe_id="cur")],
            cells={
                "Storage": {
                    "assets": 14, "components": 14, "best": "",
                    "comparable_any": False,
                    "explain": "14 component(s): 11 with no version read, "
                               "3 with no approved target",
                    "unscored": "no target or version",
                    "recipes": [
                        line("Current", "j1", None, 0, 0, 0, 14,
                             not_detected=11, not_in_recipe=3, recipe_id="cur")
                    ],
                }
            },
        ),
    )
    html = page(client)
    assert "no target or version" in html, (
        "a reader must be told which of the two gaps this is without hovering"
    )
    assert "11 with no version read" in html, (
        "the reader has to be able to tell a gap in the recipe from a gap in "
        "the inventory"
    )
    assert "14 component(s)" in html


def test_the_legend_explains_every_state_a_cell_can_be_in(client):
    publish(portal.overview_store, TenantState(tenant_id="t1", name="SAP-one",
                                               status="done"))
    html = page(client)
    for phrase in ("no assets", "no target", "no version"):
        assert phrase in html


# --------------------------------------------------------- multi-line cells


def test_a_cell_shows_a_line_per_recipe(client):
    publish(
        portal.overview_store,
        TenantState(
            tenant_id="t1", name="SAP-one", status="done",
            recipes=[
                line("Current", "j1", 45.0, 9, 20, 20, 25, recipe_id="cur"),
                line("Legacy", "j2", 30.0, 3, 10, 10, 25, recipe_id="leg"),
            ],
            best="cur",
            cells={
                "Server": {
                    "assets": 4, "components": 25, "best": "cur",
                    "comparable_any": True, "explain": "",
                    "recipes": [
                        line("Current", "j1", 45.0, 9, 20, 20, 25, recipe_id="cur"),
                        line("Legacy", "j2", 30.0, 3, 10, 10, 25, recipe_id="leg"),
                    ],
                }
            },
        ),
    )
    html = page(client)
    assert "45.0%" in html and "30.0%" in html
    assert html.count("Current") >= 2 and html.count("Legacy") >= 2
    assert "is-best" in html, "the baseline the account follows is marked"


def test_a_recipe_with_nothing_to_say_about_a_cell_says_no_target(client):
    publish(
        portal.overview_store,
        TenantState(
            tenant_id="t1", name="SAP-one", status="done",
            recipes=[line("Current", "j1", 45.0, 9, 20, 20, 25, recipe_id="cur")],
            best="cur",
            cells={
                "PDU": {
                    "assets": 2, "components": 4, "best": "cur",
                    "comparable_any": True, "explain": "",
                    "recipes": [
                        line("Current", "j1", 100.0, 4, 4, 4, 4, recipe_id="cur"),
                        line("Legacy", "j2", None, 0, 0, 0, 4, recipe_id="leg"),
                    ],
                }
            },
        ),
    )
    assert "no target" in page(client)


def test_the_column_is_called_recipe(client):
    """It was "Follows", which named the automatic reading and nothing else.

    The column is now the choice itself, so it is named after what it holds.
    """
    publish(
        portal.overview_store,
        TenantState(
            tenant_id="t1", name="SAP-one", status="done",
            recipes=[
                line("Current", "j1", 45.0, 9, 20, 20, 25, recipe_id="cur"),
                line("Legacy", "j2", 90.0, 9, 10, 10, 25, recipe_id="leg"),
            ],
            best="cur",
        ),
    )
    html = page(client)
    assert "<th class=\"col-recipe\">Recipe</th>" in html
    assert "Follows" not in html
    # Left on automatic the reading is still shown, in the dropdown itself.
    assert "Automatic" in html and "Current" in html


# ------------------------------------------------------------ recipe library


def test_the_library_is_listed_with_its_names(client):
    publish(portal.overview_store, TenantState(tenant_id="t1", name="SAP-one",
                                               status="done"))
    html = page(client)
    assert "Approved recipes" in html
    assert "march.xlsx" in html and "jan.xlsx" in html
    assert "Add another recipe" in html


def test_a_row_being_measured_keeps_its_figures(client):
    """A sweep must not empty most of the grid while it runs.

    Every account is re-measured on a sweep, so hiding a row's cells while it
    is in flight blanked whatever the sweep was working on - and the figures
    from the last sweep are still true until the new ones land.
    """
    publish(
        portal.overview_store,
        TenantState(
            tenant_id="t1", name="SAP-one", status="running",
            recipes=[line("Current", "j1", 87.9, 51, 58, 58, 58, recipe_id="cur")],
            best="cur",
            cells={
                "Server": {
                    "assets": 9, "components": 34, "best": "cur",
                    "comparable_any": True, "explain": "", "unscored": "",
                    "recipes": [line("Current", "j1", 88.2, 30, 34, 34, 34,
                                     recipe_id="cur")],
                }
            },
        ),
    )
    portal.overview_store.status = "running"
    html = page(client)
    assert "measuring" in html
    assert "87.9%" in html and "88.2%" in html


def test_a_pending_account_renders_without_stray_marks(client):
    publish(
        portal.overview_store,
        TenantState(tenant_id="t1", name="SAP-one", status="pending"),
    )
    portal.overview_store.status = "running"
    html = page(client)
    assert "queued" in html


# ------------------------------------ choosing the recipes an account is judged by

# Every account is measured against every recipe, so which of them its row is
# judged by is a choice - and often more than one of them, read side by side.
# The thing that must not go wrong is one row's choice reaching another row.


def matrix(html: str) -> str:
    """Just the grid.

    The estate-wide figures in the recipe library table are deliberately
    per-recipe whatever any account has been pinned to, so counting a
    percentage across the whole page would count those too.
    """
    start = html.index('<table class="results matrix">')
    return html[start:html.index("</table>", start)]


def both_lines():
    return [
        line("Current", "j1", 45.0, 9, 20, 20, 25, recipe_id="cur"),
        line("Legacy", "j2", 90.0, 18, 20, 20, 25, recipe_id="leg"),
    ]


def two_recipe_row(**overrides):
    payload = dict(
        tenant_id="t1", name="SAP-one", status="done",
        recipes=both_lines(), best="cur",
        cells={
            "Server": {
                "assets": 4, "components": 25, "best": "cur",
                "comparable_any": True, "explain": "", "unscored": "",
                "recipes": both_lines(),
            }
        },
    )
    payload.update(overrides)
    return TenantState(**payload)


def menu(html: str, tenant_id: str = "t1") -> str:
    """The recipe menu for one account, markup and all."""
    start = html.index(f'action="/overview/{tenant_id}/recipe"')
    return html[start:html.index("</form>", start)]


def ticked(html: str, tenant_id: str = "t1") -> list:
    """Which recipes the menu comes up with ticked."""
    boxes = re.findall(
        r'<input type="checkbox" name="recipe_id" value="([^"]*)"\s*([^>]*)>',
        menu(html, tenant_id),
    )
    return [value for value, rest in boxes if "checked" in rest]


def test_every_account_gets_a_menu_of_the_uploaded_recipes(client):
    publish(portal.overview_store, two_recipe_row())
    html = page(client)
    assert 'name="recipe_id"' in html
    assert 'value="cur"' in html and 'value="leg"' in html
    assert "/overview/t1/recipe" in html, (
        "the form posts to a URL naming the account, so a choice cannot be "
        "applied to the wrong one"
    )


def test_the_menu_offers_a_new_recipe(client):
    publish(portal.overview_store, two_recipe_row())
    html = page(client)
    assert "Add a new recipe" in html
    assert 'id="add-recipe"' in html, "and somewhere for it to lead"


def test_the_menu_comes_up_on_the_chosen_recipes(client):
    portal.overview_store._selections["t1"] = ["cur", "leg"]
    publish(portal.overview_store, two_recipe_row())
    assert ticked(page(client)) == ["cur", "leg"]


def test_the_menu_comes_up_on_one_when_one_was_chosen(client):
    portal.overview_store._selections["t1"] = ["leg"]
    publish(portal.overview_store, two_recipe_row())
    assert ticked(page(client)) == ["leg"]


def test_nothing_is_ticked_when_nothing_was_chosen(client):
    publish(portal.overview_store, two_recipe_row())
    assert ticked(page(client)) == []
    assert "Automatic" in menu(page(client))


def test_a_row_shows_only_the_recipes_it_was_given(client):
    portal.overview_store._selections["t1"] = ["leg"]
    publish(portal.overview_store, two_recipe_row())
    grid = matrix(page(client))
    assert "90.0%" in grid
    assert "45.0%" not in grid, (
        "the row was pinned to Legacy, so Current's figures are not what it "
        "is being judged by"
    )


def test_two_chosen_recipes_each_keep_their_own_figures(client):
    """The point of choosing several: one line each, not one blended figure."""
    portal.overview_store._selections["t1"] = ["cur", "leg"]
    publish(portal.overview_store, two_recipe_row())
    grid = matrix(page(client))
    assert "45.0%" in grid and "90.0%" in grid
    assert "Current" in grid and "Legacy" in grid, (
        "with more than one line the lines have to say which recipe they are"
    )


def test_an_account_left_alone_still_shows_every_recipe(client):
    publish(portal.overview_store, two_recipe_row())
    grid = matrix(page(client))
    assert "45.0%" in grid and "90.0%" in grid
    assert "Automatic" in grid


def test_one_accounts_choice_does_not_reach_another(client):
    """Both rows come from one snapshot, so this is the leak to guard against."""
    portal.overview_store._selections["t1"] = ["leg"]
    store = portal.overview_store
    store._tenants = {
        "t1": two_recipe_row(),
        "t2": two_recipe_row(tenant_id="t2", name="SAP-two"),
    }
    store._order = ["t1", "t2"]
    store.status = "done"

    grid = matrix(page(client))
    # SAP-one shows Legacy alone - one line in its Overall cell and one in its
    # Server cell. SAP-two, untouched, still shows both recipes in each.
    assert grid.count("45.0%") == 2, "Current's figures belong to SAP-two only"
    assert grid.count("90.0%") == 4


def test_a_row_pinned_to_a_recipe_it_has_no_figures_for_says_so(client):
    portal.overview_store._selections["t1"] = ["leg"]
    publish(
        portal.overview_store,
        two_recipe_row(
            recipes=[line("Current", "j1", 45.0, 9, 20, 20, 25, recipe_id="cur")],
            cells={},
        ),
    )
    html = page(client)
    assert "no figures against" in html and "Legacy" in html, (
        "an empty row reads as a broken page rather than as a check in flight"
    )


# ------------------------------------------------------------------ the route


def test_choosing_recipes_records_them(client, monkeypatch):
    asked = []
    monkeypatch.setattr(
        portal.overview_store, "focus",
        lambda tenant_id: asked.append(tenant_id) or "checking",
    )
    publish(portal.overview_store, two_recipe_row())

    response = client.post(
        "/overview/t1/recipe",
        data={"recipe_id": ["leg", "cur"], "_csrf": "test-csrf"},
    )
    assert response.status_code == 302
    assert portal.overview_store.selection("t1") == ["cur", "leg"], (
        "kept in library order, so every row reads the same way"
    )
    assert asked == ["t1"], "the account is checked straight away"


def test_choosing_one_recipe_still_works(client, monkeypatch):
    monkeypatch.setattr(portal.overview_store, "focus", lambda tenant_id: "checking")
    publish(portal.overview_store, two_recipe_row())
    client.post("/overview/t1/recipe", data={"recipe_id": "leg", "_csrf": "test-csrf"})
    assert portal.overview_store.selection("t1") == ["leg"]


def test_ticking_nothing_returns_the_account_to_automatic(client, monkeypatch):
    monkeypatch.setattr(portal.overview_store, "focus", lambda tenant_id: "checking")
    portal.overview_store._selections["t1"] = ["leg"]
    client.post("/overview/t1/recipe", data={"_csrf": "test-csrf"})
    assert portal.overview_store.selection("t1") == []


def test_a_recipe_that_is_not_in_the_library_is_refused(client, monkeypatch):
    monkeypatch.setattr(portal.overview_store, "focus", lambda tenant_id: "checking")
    client.post(
        "/overview/t1/recipe", data={"recipe_id": "made-up", "_csrf": "test-csrf"}
    )
    assert portal.overview_store.selection("t1") == []


def test_one_bad_recipe_refuses_the_whole_choice(client, monkeypatch):
    """Storing the half that is valid would be a choice nobody made."""
    monkeypatch.setattr(portal.overview_store, "focus", lambda tenant_id: "checking")
    client.post(
        "/overview/t1/recipe",
        data={"recipe_id": ["leg", "made-up"], "_csrf": "test-csrf"},
    )
    assert portal.overview_store.selection("t1") == []


def test_asking_for_a_new_recipe_leads_to_the_upload_form(client, monkeypatch):
    """The no-script path: with scripting the form opens without posting."""
    called = []
    monkeypatch.setattr(
        portal.overview_store, "focus", lambda tenant_id: called.append(tenant_id)
    )
    response = client.post(
        "/overview/t1/recipe", data={"recipe_id": "__new__", "_csrf": "test-csrf"}
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("#add-recipe")
    assert called == [], "nothing was chosen, so nothing is checked"


def test_a_choice_without_a_csrf_token_is_rejected(client):
    response = client.post("/overview/t1/recipe", data={"recipe_id": "leg"})
    assert response.status_code == 400
