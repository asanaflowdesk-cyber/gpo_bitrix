from eqazyna_bitrix.config.assignment import is_assignment_load_deal, parse_stage_ids, stage_id_matches


def test_stage_id_suffix_matches_category_stage():
    allowed = parse_stage_ids("NEW,EXECUTING")

    assert stage_id_matches("NEW", allowed)
    assert stage_id_matches("C2:NEW", allowed)
    assert stage_id_matches("EXECUTING", allowed)
    assert stage_id_matches("C2:EXECUTING", allowed)


def test_assignment_load_counts_only_new_and_in_work_stages():
    allowed = parse_stage_ids("NEW,EXECUTING")

    assert is_assignment_load_deal({"STAGE_ID": "NEW", "CLOSED": "N"}, allowed)
    assert is_assignment_load_deal({"STAGE_ID": "C2:EXECUTING", "CLOSED": "N"}, allowed)
    assert not is_assignment_load_deal({"STAGE_ID": "PREPARATION", "CLOSED": "N"}, allowed)
    assert not is_assignment_load_deal({"STAGE_ID": "C2:UC_DOCUMENTS", "CLOSED": "N"}, allowed)
    assert not is_assignment_load_deal({"STAGE_ID": "LOSE", "CLOSED": "Y"}, allowed)
