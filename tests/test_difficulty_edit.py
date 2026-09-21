import json
import unittest
import test_manual_queue
from pairwise_console.api import Handler
from unittest.mock import MagicMock

class DifficultyEditTests(unittest.TestCase):
    def setUp(self):
        self.f=test_manual_queue.ManualQueueTests();self.f.setUp();self.db=self.f.db;self.s=self.f.service;self.pid=self.f.pid
        self.db.execute("""INSERT INTO difficulty_reviews(id,pair_id,original_difficulty,a_difficulty,b_difficulty,
                          assessed_difficulty,reason,evidence_json,a_commit_sha,b_commit_sha,status,created_at,updated_at)
                          VALUES('d',?,'困难','中等','中等','中等','原复评依据','["原始证据"]',?,?,'rejected',?,?)""",
                        (self.pid,'a'*40,'b'*40,self.f.stamp,self.f.stamp))
    def tearDown(self):self.f.tearDown()
    def test_edit_preserves_original_findings_and_audits_manual_change(self):
        original=self.db.one("SELECT prompt FROM tasks WHERE id='task-1'")['prompt']
        result=self.s.edit_pair_difficulty(self.pid,'地狱','人工确认复杂状态边界')
        r=result['difficulty_review']
        self.assertEqual((r['assessed_difficulty'],r['a_difficulty'],r['b_difficulty'],r['original_difficulty']),('地狱','中等','中等','困难'))
        self.assertEqual(r['evidence_json'],'["原始证据"]');self.assertEqual(result['task']['prompt'],original)
        self.assertEqual(result['task']['difficulty'],'地狱')
        audit=json.loads(self.db.one("SELECT detail_json FROM audit_events WHERE event_type='difficulty.manually_edited'")['detail_json'])
        self.assertEqual(audit['previousReview']['reason'],'原复评依据')
        h=Handler.__new__(Handler);h.server=MagicMock(db=self.db)
        rows=h._pairs_page({'difficulty':['地狱']})['items'];self.assertEqual(rows[0]['assessed_difficulty'],'地狱')
    def test_submitted_or_running_review_cannot_be_modified(self):
        self.db.execute("UPDATE delivery_submissions SET remote_id='123',status='qc_passed'")
        with self.assertRaisesRegex(ValueError,'已提交'):self.s.edit_pair_difficulty(self.pid,'困难')
        self.db.execute("UPDATE delivery_submissions SET remote_id='',status='needs_review'")
        self.db.execute("UPDATE difficulty_reviews SET status='running'")
        with self.assertRaisesRegex(ValueError,'复评'):self.s.edit_pair_difficulty(self.pid,'困难')
        with self.assertRaisesRegex(ValueError,'请选择'):self.s.edit_pair_difficulty(self.pid,'invalid')
    def test_rejected_pair_resumes_only_with_matching_passed_artifacts(self):
        self.db.execute("UPDATE pairs SET stage='difficulty_rejected' WHERE id=?",(self.pid,))
        with self.assertRaisesRegex(ValueError,'Docker'):self.s.edit_pair_difficulty(self.pid,'困难')
        for side in ('A','B'):
            self.db.execute("INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at) VALUES(?,?,?,?,'passed',?,?)",('c-'+side,self.pid,side,side.lower()*40,self.f.stamp,self.f.stamp))
        result=self.s.edit_pair_difficulty(self.pid,'困难')
        self.assertEqual(result['stage'],'recording');self.assertEqual(result['delivery']['status'],'needs_review')
    def test_lower_rating_stays_rejected_and_original_review_evidence_remains(self):
        result=self.s.edit_pair_difficulty(self.pid,'简单')
        self.assertEqual(result['difficulty_review']['status'],'rejected')
        self.assertEqual(result['difficulty_review']['evidence_json'],'["原始证据"]')
