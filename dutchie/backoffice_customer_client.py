"""Captured Backoffice customer profile calls, authenticated by the employee session."""

from __future__ import annotations

from .session import PosClient


def _text(value) -> str:
    return str(value or "").strip()


def _date(value) -> str:
    text = _text(value)[:10]
    return f"{text}T08:00:00.000Z" if len(text) == 10 else _text(value)


def build_customer_payload(existing: dict, scan: dict, customer_type_id: int | None = None) -> dict:
    """Full captured update-customer shape, keeping fields the scan cannot supply."""
    code = _text(existing.get("Code"))
    if not code:
        raise ValueError("Dutchie customer Code is required for a Backoffice update")

    def pick(scan_key, dutchie_key, default=""):
        return _text(scan.get(scan_key)) or existing.get(dutchie_key) or default

    first = pick("first_name", "FirstName")
    middle = pick("middle_name", "MiddleName")
    last = pick("last_name", "LastName")
    name = " ".join(part for part in (first, middle, last) if part)
    phone = pick("phone", "CellPhone") or _text(existing.get("Phone"))
    return {
        "Address1": pick("address", "Address1"),
        "Address2": pick("address2", "Address2"),
        "Address3": existing.get("Address3") or "",
        "CaregiverDOB": existing.get("CaregiverDOB") or "",
        "CaregiverEmail": existing.get("CaregiverEmail") or "",
        "CaregiverExpirationDate": existing.get("CaregiverExpirationDate") or "",
        "CaregiverFirstName": existing.get("CaregiverFirstName") or "",
        "CaregiverLastName": existing.get("CaregiverLastName") or "",
        "CaregiverMJStateIdNo": existing.get("CaregiverMJStateIdNo") or "",
        "CaregiverNotes": existing.get("CaregiverNotes") or "",
        "CaregiverPhone": existing.get("CaregiverPhone") or "",
        "CaregiverStartDate": existing.get("CaregiverStartDate") or "",
        "CellPhone": phone,
        "City": pick("city", "City"),
        "Code": code,
        "CustomerTypeId": int(customer_type_id or scan.get("customer_type_id") or existing.get("CustomerTypeId") or 2),
        "DriversLicense": pick("id_number", "DriversLicense"),
        "DriversLicenseExpiration": _date(scan.get("id_expiration")) or existing.get("DriversLicenseExpiration") or "",
        "EmailAddress": pick("email", "EmailAddress"),
        "Gender": pick("gender", "Gender"),
        "InventoryPercent": existing.get("InventoryPercent") or 100,
        "MiddleName": middle,
        "MJStateIDNo": pick("mjstateidno", "MJStateIDNo"),
        "MJStateIDStartDate": existing.get("MJStateIDStartDate") or "",
        "Name": name or _text(existing.get("Name")),
        "NamePrefix": existing.get("NamePrefix") or "",
        "NameSuffix": existing.get("NameSuffix") or "",
        "Notes": existing.get("Notes") or "",
        "PatientDOB": _date(scan.get("birth_date")) or existing.get("PatientDOB") or "",
        "Phone": phone,
        "PostalCode": pick("postal_code", "PostalCode"),
        "PrimaryQualifyingConditionId": existing.get("PrimaryQualifyingConditionId") or "",
        "State": pick("state", "State"),
        "StateExpiration": _date(scan.get("id_expiration")) or existing.get("StateExpiration") or "",
        "Status": existing.get("Status") or 1,
        "CustomerGroupIds": existing.get("CustomerGroupIds") or "",
        "Group1Id": existing.get("Group1Id"),
        "Group2Id": existing.get("Group2Id"),
        "Group3Id": existing.get("Group3Id"),
        "Group4Id": existing.get("Group4Id"),
        "Group5Id": existing.get("Group5Id"),
        "FirstName": first,
        "LastName": last,
    }


class BackofficeCustomerClient(PosClient):
    """Only the employee-authenticated Backoffice customer calls in the capture."""

    def __init__(self, store, timeout: float = 30.0):
        super().__init__(store, timeout)
        self.base_origin = store.base_url.rstrip("/")

    def employee_details(self) -> dict:
        return self.post("/api/posv3/user/EmployeeLoginDetails", self.session_block())

    def customer_types(self) -> list[dict]:
        data = self.post("/api/customers/get-customer-types-enabled-for-lsp", self.session_block())
        rows = data.get("Data") or []
        return rows if isinstance(rows, list) else []

    def search_customers(self, name: str, page_size: int = 25) -> list[dict]:
        query = _text(name)
        if len(query) < 2:
            return []
        body = {
            "PageSize": min(max(int(page_size), 1), 100),
            "SortBy": "Id",
            "SortDirection": "ASC",
            "Filters": [
                {"Key": "Name", "Value": query, "Operator": "Like"},
                {"Key": "StatusName", "Value": "Archived", "Operator": "NotEqual"},
            ],
            "IncludeTotalCount": True,
            **self.session_block(),
        }
        data = self.post("/api/customers/get-customers-list-keyset", body)
        rows = (data.get("Data") or {}).get("Data") or []
        return rows if isinstance(rows, list) else []

    def find_customer(self, acct_id, name: str) -> dict | None:
        wanted = str(acct_id or "")
        for row in self.search_customers(name):
            if str(row.get("Id") or "") == wanted:
                return row
        return None

    def update_customer(self, existing: dict, scan: dict, customer_type_id: int | None = None) -> dict:
        return self.post(
            "/api/posv3/maintenance/update-customer",
            {**build_customer_payload(existing, scan, customer_type_id), **self.session_block()},
        )

    def patient_notes(self, acct_id: int) -> dict:
        return self.post("/api/v2/guest/View_Patient_Notes",
                         {"Guest_id": int(acct_id), **self.session_block()})

    def patient_files(self, acct_id: int) -> dict:
        return self.post("/api/v2/guest/get-file-list",
                         {"PatientId": int(acct_id), **self.session_block()})

    def qualifying_conditions(self, acct_id: int) -> dict:
        return self.post("/api/v2/guest/get-qualifying-conditions",
                         {"Guest_id": int(acct_id), **self.session_block()})
