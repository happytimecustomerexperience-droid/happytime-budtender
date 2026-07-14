from dutchie import pos_read
from dutchie.pos_read import PosReadClient
from pos import views
from pos.views import _pick_guest, _resolve_or_create, _resolve_scanned_customer


class _Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def test_public_customer_lookup_and_sparse_update_shapes(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return _Response({"customerId": 42})

    monkeypatch.setattr(pos_read, "http_post", fake_post)
    client = PosReadClient("location-key")
    client.customer_lookup(phone="5095550100", last_name="Doe", birth_date="1990-01-01")
    client.save_customer(customer_id=42, address="123 Main St", city="Yakima", state="WA",
                         postal_code="98901", phone="5095550100")

    assert calls[0] == (
        "https://api.pos.dutchie.com/customer/customerLookup",
        {"phone": "5095550100", "lastName": "Doe", "dateOfBirth": "1990-01-01"},
    )
    assert calls[1] == (
        "https://api.pos.dutchie.com/customer/customer",
        {"customerId": 42, "address1": "123 Main St", "city": "Yakima", "state": "WA",
         "postalCode": "98901", "phone": "5095550100"},
    )


def test_pos_fuzzy_lookup_prefers_exact_identity():
    guests = [
        {"acct_id": 20, "name": "Jane Doe", "phone": "5095559999"},
        {"acct_id": 10, "name": "Jane Doe", "phone": "5095550100"},
    ]
    assert _pick_guest(guests, phone="5095550100", name="Jane Doe")["acct_id"] == 10


def test_public_identity_match_fills_phone_for_profile_history():
    class RegisterClient:
        def guest_search(self, query):
            raise AssertionError("public match should win")

    class RestClient:
        def customer_lookup(self, **kwargs):
            return {"customerId": 42, "firstName": "Jane", "lastName": "Doe",
                    "phone": "1-509-555-0100", "address1": "123 Main St"}

    scan = {"first_name": "Jane", "last_name": "Doe", "birth_date": "1990-01-01"}
    acct, name, phone, how = _resolve_or_create(RegisterClient(), scan, "", RestClient())
    assert (acct, name, phone, how) == (42, "Jane Doe", "1-509-555-0100", "public")
    assert scan["address"] == "123 Main St"


def test_scanned_name_lookup_returns_all_possible_matches():
    class RegisterClient:
        def guest_search(self, query):
            return {"Data": [
                {"Guest_id": 20, "Name": "Jane Doe", "PhoneNo": "5095550102"},
                {"Guest_id": 10, "Name": "Jane Doe", "PhoneNo": "5095550101"},
            ]}

    scan = {"accts_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe"}
    acct, name, phone, how, matches = _resolve_scanned_customer(RegisterClient(), scan, "")
    assert acct is None and name is None and phone == "" and how == "name_matches"
    assert [row["acct_id"] for row in matches] == [10, 20]
    assert all(row["possible_name_match"] for row in matches)


def test_customer_sync_reloads_backoffice_customer_after_write(monkeypatch):
    class Backoffice:
        def __init__(self):
            self.finds = 0
            self.updated = []

        def find_customer(self, acct_id, name):
            self.finds += 1
            return {"Id": acct_id, "Code": "C-7", "FirstName": "Jane", "LastName": "Doe",
                    "CellPhone": "5095550100", "Address1": "123 Main" if self.finds > 1 else "Old"}

        def update_customer(self, row, scan):
            self.updated.append((row, dict(scan)))

    client = Backoffice()
    monkeypatch.setattr(views, "_backoffice_client", lambda store: client)
    scan = {"first_name": "Jane", "last_name": "Doe", "accts_name": "Jane Doe", "phone": "5095550100"}

    refreshed = views._sync_customer_to_dutchie(object(), scan, 7)
    assert refreshed["Address1"] == "123 Main"
    assert client.finds == 2 and len(client.updated) == 1
    assert scan["address"] == "123 Main"
