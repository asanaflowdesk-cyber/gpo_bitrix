from eqazyna_bitrix.reassign_by_contact_id_batch import Bitrix, BitrixError


def test_contact_company_discovery_does_not_silently_ignore_relation_error():
    class Fake(Bitrix):
        def __init__(self):
            pass

        def call_form(self, method, params=None):
            raise BitrixError("ACCESS_DENIED")

    bx = Fake()
    try:
        bx.get_contact_company_ids({"ID": "178", "COMPANY_ID": "738"})
    except BitrixError as exc:
        assert "ACCESS_DENIED" in str(exc)
    else:
        raise AssertionError("relation discovery error must stop the workflow")
