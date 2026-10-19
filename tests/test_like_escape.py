import pytest
from toolframe_sdk import PluginError

from toolframe_sql_postgres.sql import prepare


def test_parameterized_literal_search_accepts_parser_generated_escape_function():
    statement, values = prepare(
        "SELECT label FROM entries WHERE LOWER(label) LIKE LOWER(?) ESCAPE '\\'",
        [r"%100\% spicy\_ soup%"],
        "query",
    )
    assert "pg_catalog.like_escape" in statement
    assert values == [r"%100\% spicy\_ soup%"]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_catalog.pg_read_file(?) FROM entries",
        "SELECT public.like_escape(label, ?) FROM entries",
        "SELECT label FROM entries WHERE label LIKE pg_sleep(?) ESCAPE '\\'",
    ],
)
def test_escape_support_does_not_admit_other_functions(query):
    with pytest.raises(PluginError) as error:
        prepare(query, ["private"], "query")
    assert error.value.code == "sql_not_supported"
    assert "private" not in str(error.value)
