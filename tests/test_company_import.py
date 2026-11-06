"""CSV company import: header matching, encodings, HTML descriptions."""

import pytest

from harvey.company_import import domain_from_url, parse_companies, read_csv_text


def test_domain_from_url():
    assert domain_from_url("www.Example.com") == "example.com"
    assert domain_from_url("https://www.example.org/about?x=1") == "example.org"
    assert domain_from_url("shop.example.net") == "shop.example.net"
    assert domain_from_url("") == ""
    assert domain_from_url("not a site") == ""


def test_parses_export_with_html_and_odd_headers():
    text = (
        "name,slug\xa0,website,description,headquarters,is_active,,\r\n"
        'Acme Corp,,www.acme.example,"<p>Makes&nbsp;anvils.</p>\n\n<p>Ships worldwide.</p>",United States,1,,\r\n'
        "No Site Ltd,,,just a name,India,1,,\r\n"
        "Acme Again,,https://acme.example/,dup,United States,1,,\r\n"
    )
    companies, skipped = parse_companies(text, default_industry="Manufacturing")
    assert len(companies) == 1
    c = companies[0]
    assert (c.name, c.domain, c.website) == ("Acme Corp", "acme.example", "https://acme.example")
    assert c.description == "Makes anvils. Ships worldwide."
    assert (c.location, c.industry, c.source) == ("United States", "Manufacturing", "csv_import")
    assert len(skipped) == 2


def test_requires_a_website_column():
    with pytest.raises(ValueError):
        parse_companies("name,country\nAcme,US\n")


def test_reads_cp1252_files(tmp_path):
    path = tmp_path / "list.csv"
    path.write_bytes("name,website\r\nCaf\xe9 Co,cafe.example\r\n".encode("cp1252"))
    companies, _ = parse_companies(read_csv_text(path))
    assert companies[0].name == "Café Co"


def test_inactive_status_rows_are_skipped():
    text = ("Company Name,Website,Status\n"
            "Live Co,live.example,Active\n"
            "Gone Co,gone.example,Inactive\n"
            "Zero Co,zero.example,0\n")
    companies, skipped = parse_companies(text)
    assert [c.domain for c in companies] == ["live.example"]
    assert len(skipped) == 2 and "Inactive" in skipped[0]


@pytest.mark.asyncio
async def test_import_then_enrich_without_network(tmp_path):
    from harvey.company_import import enrich, import_companies
    from harvey.state import StateManager

    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    companies, _ = parse_companies("name,website\nAcme,acme.example\n")
    added, existing = await import_companies(state, companies)
    assert len(added) == 1 and added[0].id and not existing
    again, existing = await import_companies(state, parse_companies("name,website\nAcme,acme.example\n")[0])
    assert not again and "already" in existing[0]

    stages = []
    report = await enrich(state, None, ["acme.example"], read_sites=False,
                          sync_mysql=False, progress=stages.append)
    assert (report.profiled, report.people_added, report.errors) == (0, 0, [])

    # A company that already has max_people contacts costs no Hunter search.
    from harvey.models.prospect import Prospect
    await state.add_prospect(Prospect(company_id=added[0].id, first_name="Pat", last_name="Example",
                                      title="CEO", email="pat@acme.example", company_name="Acme"))
    report = await enrich(state, None, ["acme.example"], read_sites=False, sync_mysql=False,
                          hunter_key="key", max_people=1)
    assert report.hunter_searches == 0 and "already has 1" in report.people[0]["note"]


def test_discovery_industry_override():
    from types import SimpleNamespace
    from harvey.collectors.discover import build_queries

    icp = SimpleNamespace(industries=["SaaS"], geography=["Denver, CO"],
                          keyword_variants={}, geo_coordinates={})
    config = SimpleNamespace(icp=icp, product=SimpleNamespace(name="X"))
    assert [q.term for q in build_queries(config)] == ["SaaS"]
    assert [q.term for q in build_queries(config, industries=["Dental clinic", " "])] == ["Dental clinic"]
