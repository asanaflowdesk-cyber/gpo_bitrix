from __future__ import annotations

from eqazyna_bitrix.reassign_founder_package_random import (
    build_timeline_comment,
    candidate_manager_ids,
    find_cross_package_conflicts,
    parse_founder_contact_ids,
    stable_candidate_order,
)


def test_parse_founder_ids_accepts_commas_spaces_and_newlines() -> None:
    assert parse_founder_contact_ids("178, 241\n315;178") == ["178", "241", "315"]


def test_candidate_pool_excludes_explicit_and_current_owner() -> None:
    result = candidate_manager_ids(
        active_manager_ids=[72, 74, 76, 78],
        explicit_excluded_ids={"74", "78"},
        current_founder_owner_id="72",
    )
    assert result == ["76"]


def test_stable_candidate_order_is_repeatable_and_keeps_all_candidates() -> None:
    first = stable_candidate_order("178", ["72", "74", "76", "78"])
    second = stable_candidate_order("178", ["78", "76", "74", "72"])
    assert first == second
    assert sorted(first, key=int) == ["72", "74", "76", "78"]


def test_cross_package_conflict_is_detected_before_write() -> None:
    packages = [
        {
            "founder_contact_id": "178",
            "target_user_id": "72",
            "rows": [
                {"entity_type": "company", "entity_id": "208", "action_status": "pending_update"}
            ],
        },
        {
            "founder_contact_id": "241",
            "target_user_id": "74",
            "rows": [
                {"entity_type": "company", "entity_id": "208", "action_status": "pending_update"}
            ],
        },
    ]
    conflicts = find_cross_package_conflicts(packages)
    assert conflicts == [
        {
            "entity_type": "company",
            "entity_id": "208",
            "first_founder_contact_id": "178",
            "first_target_user_id": "72",
            "second_founder_contact_id": "241",
            "second_target_user_id": "74",
        }
    ]


def test_same_shared_entity_and_same_target_is_not_conflict() -> None:
    packages = [
        {
            "founder_contact_id": "178",
            "target_user_id": "72",
            "rows": [
                {"entity_type": "company", "entity_id": "208", "action_status": "pending_update"}
            ],
        },
        {
            "founder_contact_id": "241",
            "target_user_id": "72",
            "rows": [
                {"entity_type": "company", "entity_id": "208", "action_status": "pending_update"}
            ],
        },
    ]
    assert find_cross_package_conflicts(packages) == []


def test_timeline_comment_identifies_previous_and_new_owner() -> None:
    rows = [
        {
            "entity_type": "company",
            "action_status": "owner_changed_and_verified",
            "before_owner_id": "86",
        },
        {
            "entity_type": "deal",
            "action_status": "owner_changed_and_verified",
            "before_owner_id": "86",
        },
        {
            "entity_type": "contact",
            "action_status": "excluded",
            "before_owner_id": "86",
        },
    ]
    comment = build_timeline_comment(
        rows=rows,
        founder_contact_id="178",
        founder_owner_before="86",
        target_user_id="54",
        target_user_name="Пак Светлана",
        user_labels={"86": "Толекбергенов Еркебулан", "72": "Исаев Асет"},
        explicit_excluded_manager_ids={"72"},
        company_ids={"208", "210"},
        batch_size=12,
    )
    assert "Толекбергенов Еркебулан (ID 86)" in comment
    assert "Пак Светлана (ID 54)" in comment
    assert "Исаев Асет (ID 72)" in comment
    assert "12 учредителей" in comment
    assert "#208" in comment and "#210" in comment
    assert "Исключено из переноса" in comment
