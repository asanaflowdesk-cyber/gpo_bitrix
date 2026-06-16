from eqazyna_bitrix.reassign_by_company_id_batch import build_rows, parse_bool, parse_id_set


def test_parse_id_set_accepts_common_separators_and_deduplicates():
    assert parse_id_set("10, 20;30\n20") == {"10", "20", "30"}


def test_parse_id_set_rejects_non_numeric_values():
    try:
        parse_id_set("10,abc")
    except ValueError as exc:
        assert "Invalid Bitrix ID" in str(exc)
    else:
        raise AssertionError("ValueError was not raised")


def test_parse_bool_handles_false_string_as_false():
    assert parse_bool("false", default=True) is False


def test_build_rows_marks_only_explicit_ids_as_excluded():
    company = {"ID": "100", "TITLE": "Company", "ASSIGNED_BY_ID": "1"}
    deals = {
        "200": {"ID": "200", "TITLE": "Keep", "ASSIGNED_BY_ID": "1", "COMPANY_ID": "100"},
        "201": {"ID": "201", "TITLE": "Move", "ASSIGNED_BY_ID": "1", "COMPANY_ID": "100"},
    }
    contacts = {
        "300": {"ID": "300", "LAST_NAME": "Keep", "ASSIGNED_BY_ID": "1", "COMPANY_ID": "100"},
        "301": {"ID": "301", "LAST_NAME": "Move", "ASSIGNED_BY_ID": "1", "COMPANY_ID": "100"},
    }

    rows = build_rows(
        company=company,
        deals=deals,
        contacts=contacts,
        target_user_id="9",
        target_user_name="Target",
        dry_run=False,
        excluded_company_ids=set(),
        excluded_deal_ids={"200"},
        excluded_contact_ids={"300"},
    )
    statuses = {(row["entity_type"], row["entity_id"]): row["action_status"] for row in rows}

    assert statuses[("company", "100")] == "pending_update"
    assert statuses[("deal", "200")] == "excluded"
    assert statuses[("deal", "201")] == "pending_update"
    assert statuses[("contact", "300")] == "excluded"
    assert statuses[("contact", "301")] == "pending_update"


def test_dry_run_never_marks_entities_for_update():
    rows = build_rows(
        company={"ID": "100", "TITLE": "Company", "ASSIGNED_BY_ID": "1"},
        deals={},
        contacts={},
        target_user_id="9",
        target_user_name="Target",
        dry_run=True,
        excluded_company_ids=set(),
        excluded_deal_ids=set(),
        excluded_contact_ids=set(),
    )
    assert rows[0]["action_status"] == "dry_run_planned"
    assert rows[0]["final_owner_id"] == "1"


class FakeBatchBitrix:
    batch_update_owners = __import__(
        "eqazyna_bitrix.reassign_by_company_id_batch",
        fromlist=["Bitrix"],
    ).Bitrix.batch_update_owners

    def __init__(self):
        self.commands = []
        self.phase = 0

    def batch(self, commands, halt=False):
        self.commands.append(dict(commands))
        self.phase += 1
        if self.phase == 1:
            return {"result": {key: True for key in commands}, "result_error": {}}
        return {
            "result": {
                key: {"ID": command.split("id=")[-1], "ASSIGNED_BY_ID": "9"}
                for key, command in commands.items()
            },
            "result_error": {},
        }


def test_batch_update_skips_excluded_rows_and_verifies_written_rows():
    rows = [
        {
            "entity_type": "company",
            "entity_id": "100",
            "before_owner_id": "1",
            "action_status": "pending_update",
            "error": "",
        },
        {
            "entity_type": "deal",
            "entity_id": "200",
            "before_owner_id": "1",
            "action_status": "excluded",
            "error": "",
        },
    ]
    bx = FakeBatchBitrix()

    bx.batch_update_owners(rows, target_user_id="9", verify=True)

    assert rows[0]["action_status"] == "owner_changed_and_verified"
    assert rows[0]["final_owner_id"] == "9"
    assert rows[1]["action_status"] == "excluded"
    all_commands = " ".join(command for call in bx.commands for command in call.values())
    assert "id=100" in all_commands
    assert "id=200" not in all_commands
