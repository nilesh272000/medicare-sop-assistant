import csv

import pytest

from app.domain.access import AccessPolicy, UserDirectory
from app.domain.errors import UnknownUserError
from app.domain.pii import categories, detect_patient_data
from tests.conftest import ROOT

POLICY = AccessPolicy.load(ROOT / "data/access_policy.json")
USERS = UserDirectory.load_csv(ROOT / "data/staff_users.csv")


@pytest.mark.parametrize("role,level,allowed", [
    ("front_desk", "all", True), ("front_desk", "clinical", False), ("front_desk", "hr", False),
    ("nurse", "clinical", True), ("nurse", "hr", False), ("doctor", "clinical", True),
    ("pharmacist", "hr", False), ("clinic_manager", "hr", True), ("clinic_manager", "clinical", True),
    ("hr", "hr", True), ("hr", "clinical", False),
])
def test_role_level_matrix(role, level, allowed):
    assert POLICY.can_read(role, level) is allowed


def test_access_field_mapping_is_whitespace_and_case_tolerant_but_exact():
    assert POLICY.level_for_access_field("Clinical staff (doctors,\n nurses, pharmacists)") == "clinical"
    assert POLICY.level_for_access_field("hr and clinic managers only") == "hr"
    # Unknown text must not silently map to anything (caller fails closed).
    assert POLICY.level_for_access_field("Clinical staff") is None
    assert POLICY.level_for_access_field("") is None


def test_unknown_role_gets_no_levels():
    assert POLICY.levels_for_role("janitor") == frozenset()
    assert not POLICY.can_read("janitor", "all")


def test_user_directory():
    assert USERS.get("U6001").role == "hr"
    assert USERS.get(" U2001 ").role == "nurse"
    with pytest.raises(UnknownUserError):
        USERS.get("U9999")


PII_ROWS = list(csv.DictReader(open(ROOT / "data/pii_test_inputs.csv", encoding="utf-8")))


@pytest.mark.parametrize("row", PII_ROWS, ids=[r["expected_guard_result"] for r in PII_ROWS])
def test_pii_pack_inputs(row):
    cats = categories(detect_patient_data(row["input"]))
    expected = row["expected_guard_result"]
    assert bool(cats) is expected.startswith("flag")
    for word, cat in [("MRN", "mrn"), ("Aadhaar", "aadhaar"), ("phone", "phone"), ("DOB", "date_of_birth"),
                      ("name", "name")]:
        if word in expected:
            assert cat in cats, (cats, expected)


@pytest.mark.parametrize("text,cat", [
    ("Call +91 98480 22113 tomorrow", "phone"),
    ("number 9848022113 please", "phone"),
    ("email ramesh.k@gmail.com about refund", "email"),
    ("patient name is Anita Sharma, what next?", "name"),
    ("Mr. Ramesh wants his report", "name"),
    ("UHID 778812 not found", "mrn"),
    ("born on 03-07-1990, can she register?", "date_of_birth"),
])
def test_pii_more_positives(text, cat):
    assert cat in categories(detect_patient_data(text))


@pytest.mark.parametrize("text", [
    "What is the policy for patients without ID?",
    "What does the Patient Registration SOP say about minors?",
    "Do I write the MRN or the name on form OH-05?",
    "Refunds above Rs 10,000 need whose approval?",
    "How long are outpatient records of a 15-year-old patient kept?",
    "What is the email of the Data Protection Officer, dpo@medicare-clinics.example?",
    "Within how many hours must a needle-stick injury be reported?",
    "Samples are picked up at 11:00 AM and 4:00 PM?",
])
def test_pii_no_false_positive_on_policy_questions(text):
    assert detect_patient_data(text) == []
