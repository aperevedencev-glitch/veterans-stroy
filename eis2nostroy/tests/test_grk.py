from eis2nostroy.grk import GRK, GRK_PROBABLE, NOT_GRK, UNKNOWN, classify, kvr_from_ikz, okpd_from_ikz

#        год|        ИКУ (20)       |номер  |ОКПД|КВР
IKZ_CAP = "26" + "7" * 20 + "0001001" + "4120" + "243"
IKZ_INV = "26" + "7" * 20 + "0001002" + "4299" + "414"
IKZ_CUR = "26" + "7" * 20 + "0001003" + "4339" + "244"
IKZ_IT = "26" + "7" * 20 + "0001004" + "6201" + "244"
IKZ_MULTI = "26" + "7" * 20 + "0001005" + "0000" + "243"


def test_positions():
    assert len(IKZ_CAP) == 36
    assert okpd_from_ikz(IKZ_CAP) == "41.20" and kvr_from_ikz(IKZ_CAP) == "243"


def test_classes():
    assert classify(IKZ_CAP)[0] == GRK
    assert classify(IKZ_INV)[0] == GRK
    assert classify(IKZ_CUR)[0] == GRK_PROBABLE          # 244 — может быть текущий ремонт
    assert classify(IKZ_IT)[0] == NOT_GRK
    assert classify(IKZ_MULTI, ["43.21.10.110"])[0] == GRK   # 0000 → ОКПД2 из позиций
    assert classify(IKZ_MULTI)[0] == UNKNOWN
    assert classify("", ["41.10.10.000"])[0] == NOT_GRK  # разработка проектов — не подряд
    assert classify("", ["42.21.22.110"])[0] == GRK_PROBABLE  # 223-ФЗ без КВР
