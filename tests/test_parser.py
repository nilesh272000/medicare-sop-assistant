import pytest

from app.domain.access import AccessPolicy
from app.domain.errors import IngestionError
from app.ingest.pdf_extract import extract_pdf
from app.ingest.sop_parser import parse_sop
from tests.conftest import ROOT, SOPS, UPDATE
from tests.pdf_factory import make_sop_pdf

POLICY = AccessPolicy.load(ROOT / "data/access_policy.json")

# From the README table of the original library.
EXPECTED = {
    "SOP-01": ("4.1", "all"), "SOP-02": ("2.3", "all"), "SOP-03": ("5.0", "all"), "SOP-04": ("3.0", "all"),
    "SOP-05": ("2.2", "all"), "SOP-06": ("3.2", "clinical"), "SOP-07": ("4.0", "all"), "SOP-08": ("2.0", "all"),
    "SOP-09": ("1.4", "clinical"), "SOP-10": ("3.1", "clinical"), "SOP-11": ("1.2", "all"),
    "SOP-12": ("2.5", "all"), "SOP-13": ("3.0", "hr"), "SOP-14": ("2.1", "hr"), "SOP-15": ("1.1", "all"),
}


def parse(path):
    return parse_sop(extract_pdf(str(path)), path.name, POLICY)


@pytest.mark.parametrize("path", sorted(SOPS.glob("*.pdf")), ids=lambda p: p.name[:6])
def test_header_metadata_matches_library_table(path):
    doc, chunks = parse(path)
    assert (doc.version, doc.access_level) == EXPECTED[doc.sop_id]
    assert chunks and all(c.section_no for c in chunks)
    assert [int(c.section_no) for c in chunks] == sorted(int(c.section_no) for c in chunks)
    # Boilerplate never ends up in chunks.
    for c in chunks:
        assert "Uncontrolled when printed" not in c.text
        assert "Superseded. Do not use" not in c.text
        assert "Approved by" not in c.text


def test_table_rows_are_self_contained():
    _, chunks = parse(SOPS / "SOP-05_Biomedical_Waste_Segregation_and_Disposal.pdf")
    colour = next(c for c in chunks if c.section_title == "Colour coding")
    red = next(line for line in colour.text.split("\n") if "Red bag" in line)
    assert "IV tubes" in red and "gloves" in red and "Autoclave" in red


def test_wrapped_line_starting_with_number_is_not_a_new_step():
    # "...until the patient turns / 21 or for 3 years" (regression: was split into a fake step 21)
    _, chunks = parse(SOPS / "SOP-12_Patient_Data_Privacy_and_Medical_Records_Access.pdf")
    retention = next(c for c in chunks if c.section_title == "Retention")
    assert "until the patient turns 21 or for 3 years" in retention.text


def test_numbered_steps_stay_separate_items():
    _, chunks = parse(SOPS / "SOP-04_NeedleStick_and_Sharps_Injury_Management.pdf")
    first_aid = next(c for c in chunks if c.section_title == "Immediate first aid")
    assert first_aid.text.split("\n")[1].startswith("2 Wash the area")


def test_heading_with_dots_is_detected():
    _, chunks = parse(UPDATE / "SOP-16_Fire_Safety_and_Evacuation.pdf")
    assert [c.section_title for c in chunks][1] == "When you discover fire or smoke (R.A.C.E.)"


def test_multiline_access_field_maps_to_clinical():
    doc, _ = parse(SOPS / "SOP-06_Vaccine_Cold_Chain_Management.pdf")
    assert doc.access_field.startswith("Clinical staff") and doc.access_level == "clinical"


def test_content_hash_ignores_file_bytes_and_name():
    # SOP-03 was re-exported (different bytes, same text); SOP-05 was renamed.
    a, _ = parse(SOPS / "SOP-03_Hand_Hygiene_and_Standard_Infection_Control.pdf")
    b, _ = parse(UPDATE / "SOP-03_Hand_Hygiene_and_Standard_Infection_Control.pdf")
    assert (SOPS / a.file_name).read_bytes() != (UPDATE / b.file_name).read_bytes()
    assert a.content_hash == b.content_hash
    c, _ = parse(SOPS / "SOP-05_Biomedical_Waste_Segregation_and_Disposal.pdf")
    d, _ = parse(UPDATE / "SOP-05_Biomedical_Waste_Management_Rules.pdf")
    assert c.file_name != d.file_name and c.content_hash == d.content_hash


def test_changed_sop_has_new_hash_and_change_note():
    old, _ = parse(SOPS / "SOP-02_Appointment_Scheduling_and_Slot_Management.pdf")
    new, _ = parse(UPDATE / "SOP-02_Appointment_Scheduling_and_Slot_Management.pdf")
    assert old.content_hash != new.content_hash and new.version == "2.4"
    assert "25%" in new.change_note


def test_unknown_access_value_fails_closed(tmp_path):
    p = make_sop_pdf(tmp_path / "x.pdf", access="Doctors only")
    with pytest.raises(IngestionError, match="unknown Access"):
        parse(p)


def test_synthetic_pdf_roundtrip(tmp_path):
    doc, chunks = parse(make_sop_pdf(tmp_path / "x.pdf", sop_id="SOP-91", version="2.0",
                                     access="HR and Clinic Managers only"))
    assert (doc.sop_id, doc.version, doc.access_level) == ("SOP-91", "2.0", "hr")
    assert [c.section_title for c in chunks] == ["Purpose", "Rules"]
