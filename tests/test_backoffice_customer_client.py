from dutchie.backoffice_customer_client import BackofficeCustomerClient, build_customer_payload
from dutchie.session import Store


STORE = Store(name="yakima", base_url="https://ash.backoffice.dutchie.com", pos_base_url="https://ash.pos.dutchie.com",
              org_id=1, lsp_id=2, loc_id=3, register_id=4, username="u", password="p")


def test_customer_search_replays_captured_keyset_contract():
    client = BackofficeCustomerClient(STORE)
    client.session_block = lambda: {"SessionId": "s", "LspId": "2", "LocId": "3", "OrgId": "1", "UserId": "5"}
    calls = []
    client.post = lambda path, body: calls.append((path, body)) or {"Data": {"Data": [{"Id": 7}]}}

    assert client.search_customers("Jane Doe") == [{"Id": 7}]
    path, body = calls[0]
    assert path == "/api/customers/get-customers-list-keyset"
    assert body["PageSize"] == 25 and body["SortBy"] == "Id" and body["SortDirection"] == "ASC"
    assert body["Filters"] == [
        {"Key": "Name", "Value": "Jane Doe", "Operator": "Like"},
        {"Key": "StatusName", "Value": "Archived", "Operator": "NotEqual"},
    ]


def test_customer_update_payload_keeps_existing_values_and_separates_ids():
    existing = {"Code": "C-7", "CustomerTypeId": 2, "Status": 1, "Address2": "Old Unit",
                "MJStateIDNo": "MED-7", "Name": "Old Name", "InventoryPercent": 100}
    scan = {"first_name": "Jane", "middle_name": "Q", "last_name": "Doe", "phone": "5095550100",
            "email": "jane@example.test", "address": "123 Main", "address2": "Unit 4",
            "city": "Yakima", "state": "WA", "postal_code": "98901-1234", "birth_date": "1990-01-15",
            "id_number": "DL-7", "id_expiration": "2030-01-15", "gender": "female"}

    body = build_customer_payload(existing, scan)
    assert body["Code"] == "C-7"
    assert body["Name"] == "Jane Q Doe"
    assert body["DriversLicense"] == "DL-7" and body["MJStateIDNo"] == "MED-7"
    assert body["Address2"] == "Unit 4" and body["PatientDOB"] == "1990-01-15T08:00:00.000Z"
    assert body["DriversLicenseExpiration"] == "2030-01-15T08:00:00.000Z"


def test_backoffice_profile_helpers_use_the_captured_subject_fields():
    client = BackofficeCustomerClient(STORE)
    client.session_block = lambda: {"SessionId": "s", "LspId": "2", "LocId": "3", "OrgId": "1", "UserId": "5"}
    calls = []
    client.post = lambda path, body: calls.append((path, body)) or {"Data": []}

    assert client.customer_types() == []
    client.patient_notes(7)
    client.patient_files(7)
    client.qualifying_conditions(7)

    assert calls[0][0] == "/api/customers/get-customer-types-enabled-for-lsp"
    assert calls[1] == ("/api/v2/guest/View_Patient_Notes", {"Guest_id": 7, **client.session_block()})
    assert calls[2] == ("/api/v2/guest/get-file-list", {"PatientId": 7, **client.session_block()})
    assert calls[3] == ("/api/v2/guest/get-qualifying-conditions", {"Guest_id": 7, **client.session_block()})
