import pytest
from emane_bridge import compress_ids, expand_ids, nem_ids, validate_matrix, parse_schedule_xml, build_schedule_xml, Part1Result, result_from_coords

def test_nem_ids():
    assert nem_ids(["Node_1", "Node_02", "Node_34"]) == {"Node_1": 1, "Node_02": 2, "Node_34": 34}
    with pytest.raises(ValueError):
        nem_ids(["Node_A"])
    with pytest.raises(ValueError):
        nem_ids(["Node_0"])
    with pytest.raises(ValueError):
        nem_ids(["Node_1", "Node_01"])

def test_compress_ids():
    assert compress_ids([1, 2, 3, 4]) == "1:4"
    assert compress_ids([1, 3, 5]) == "1,3,5"
    assert compress_ids([1, 2, 4, 5, 6, 8]) == "1,2,4:6,8"
    assert compress_ids([1, 2, 4, 5]) == "1,2,4,5"
    assert compress_ids([10, 11, 12]) == "10:12"

def test_expand_ids():
    assert expand_ids("1:4") == [1, 2, 3, 4]
    assert expand_ids("1, 3, 5") == [1, 3, 5]
    assert expand_ids("1, 2, 4:6, 8") == [1, 2, 4, 5, 6, 8]
    assert expand_ids("1,2, 4, 5") == [1, 2, 4, 5]
    with pytest.raises(ValueError):
        expand_ids("4:1")

def test_validate_matrix():
    order = ["Node_1", "Node_2"]
    matrix = [[1, 0], [0, 1]]
    validate_matrix(matrix, order)  # Should not raise

    with pytest.raises(ValueError):
        validate_matrix([[1, 0, 0], [0, 1, 0]], order) # wrong columns

    with pytest.raises(ValueError):
        validate_matrix([[2, 0], [0, 1]], order) # not 0 or 1

    with pytest.raises(ValueError):
        validate_matrix([[1, 1], [0, 1]], order) # node 2 in two slots

def test_xml_roundtrip():
    order = ["Node_1", "Node_2", "Node_3"]
    matrix = [[1, 0, 0], [0, 1, 1], [0, 0, 0]]
    xml = build_schedule_xml(matrix, order, 100.0, "test")
    structure, parsed_matrix = parse_schedule_xml(xml, order)
    assert parsed_matrix == matrix
    assert structure["slots"] == 3
