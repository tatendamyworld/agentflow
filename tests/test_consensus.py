import unittest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentflow.consensus import parse_finch_vote, evaluate_consensus, validate_finch_responses

class TestConsensus(unittest.TestCase):
    def test_yes_consensus(self):
        r = evaluate_consensus(["YES","YES","YES","YES","NO","NO","INCONCLUSIVE","INCONCLUSIVE"])
        self.assertEqual(r["outcome"], "CONSENSUS: YES")
    def test_split(self):
        r = evaluate_consensus(["YES","YES","YES","NO","NO","NO","INCONCLUSIVE","INCONCLUSIVE"])
        self.assertEqual(r["outcome"], "SPLIT — NEEDS MORE DATA")
    def test_all_inconclusive(self):
        r = evaluate_consensus(["INCONCLUSIVE"]*8)
        self.assertEqual(r["outcome"], "CONSENSUS: INCONCLUSIVE")
    def test_invalid(self):
        self.assertEqual(parse_finch_vote("No finding here"), "INVALID")
        self.assertEqual(parse_finch_vote(""), "INVALID")
    def test_case_insensitive(self):
        self.assertEqual(parse_finch_vote("key finding: yes"), "YES")
        self.assertEqual(parse_finch_vote("Key Finding: No"), "NO")
        self.assertEqual(parse_finch_vote("KEY FINDING:   inconclusive"), "INCONCLUSIVE")
    def test_whitespace(self):
        self.assertEqual(parse_finch_vote("Key finding:   YES  "), "YES")
    def test_missing(self):
        self.assertEqual(parse_finch_vote("---\nSome analysis without verdict"), "INVALID")
    def test_exactly_eight(self):
        with self.assertRaises(ValueError): validate_finch_responses(["x"]*7)
        with self.assertRaises(ValueError): validate_finch_responses(["x"]*9)
        validate_finch_responses(["x"]*8)
    def test_no_false_majority(self):
        # 3 YES, 3 NO, 2 INC -> max 3 <4 -> SPLIT
        r = evaluate_consensus(["YES","YES","YES","NO","NO","NO","INCONCLUSIVE","INCONCLUSIVE"])
        self.assertTrue(r["outcome"].startswith("SPLIT"))
    def test_raw_parsing(self):
        votes = ["Key finding: YES\n## Analysis\nblah"]*4 + ["Key finding: NO"]*2 + ["Key finding: INCONCLUSIVE"]*2
        r = evaluate_consensus(votes)
        self.assertEqual(r["outcome"], "CONSENSUS: YES")
    def test_no_consensus(self):
        r = evaluate_consensus(["NO","NO","NO","NO","YES","YES","YES","INCONCLUSIVE"])
        self.assertEqual(r["outcome"], "CONSENSUS: NO")

if __name__ == "__main__":
    unittest.main()
