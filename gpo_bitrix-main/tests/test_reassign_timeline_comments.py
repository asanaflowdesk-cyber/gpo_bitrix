from eqazyna_bitrix.reassign_by_company_id_batch import build_reassignment_comment as company_comment
from eqazyna_bitrix.reassign_by_contact_id_batch import build_reassignment_comment as contact_comment


def _rows():
    return [
        {
            "entity_type": "company",
            "before_owner_id": "86",
            "action_status": "owner_changed_and_verified",
        },
        {
            "entity_type": "deal",
            "before_owner_id": "86",
            "action_status": "owner_changed_and_verified",
        },
        {
            "entity_type": "contact",
            "before_owner_id": "54",
            "action_status": "excluded",
        },
    ]


def test_company_comment_identifies_previous_and_new_owner():
    comment = company_comment(
        rows=_rows(),
        company_id="738",
        founder_contact_id="178",
        founder_contact_owner_id="86",
        target_user_id="54",
        target_user_name="Пак Светлана",
        user_labels={"86": "Толекбергенов Еркебулан"},
    )

    assert "Толекбергенов Еркебулан (ID 86)" in comment
    assert "Пак Светлана (ID 54)" in comment
    assert "компаний — 1, сделок — 1, контактов — 0" in comment
    assert "Исключено из переноса" in comment
    assert "по компании #738" in comment
    assert "карточка учредителя #178" in comment


def test_contact_comment_identifies_previous_and_new_owner():
    comment = contact_comment(
        rows=_rows(),
        contact_id="178",
        source_contact_owner_id="86",
        target_user_id="54",
        target_user_name="Пак Светлана",
        user_labels={"86": "Толекбергенов Еркебулан"},
    )

    assert "Толекбергенов Еркебулан (ID 86)" in comment
    assert "Пак Светлана (ID 54)" in comment
    assert "по контакту #178" in comment
