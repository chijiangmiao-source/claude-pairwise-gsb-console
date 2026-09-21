import json
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pairwise_console.api import AppServer, Handler
from pairwise_console.config import load_config
from pairwise_console.db import Database, now_iso
from pairwise_console.service import PairwiseService


class ManualQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = replace(load_config(Path(__file__).resolve().parents[1]),
                              data_dir=self.root / 'data', db_path=self.root / 'data/db',
                              projects_dir=self.root / 'projects', old_db_path=self.root / 'old.db')
        self.db = Database(self.config.db_path)
        self.db.initialize()
        self.service = PairwiseService(self.config, self.db)
        stamp = now_iso()
        self.stamp = stamp
        self.db.execute("""INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,fingerprint,status,created_at,updated_at)
                          VALUES('task-1','test','zero_to_one','test','完整题目\n最后一行','困难','unique','ready',?,?)""", (stamp, stamp))
        self.pair = self.service.create_pair('task-1')
        self.pid = self.pair['id']
        self.db.execute("UPDATE pairs SET status='failed',stage='development_failed',baseline_sha=?,development_failure_count=2 WHERE id=?", ('a'*40, self.pid))
        self.db.execute("""INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,status,created_at,updated_at)
                          VALUES('repo-1',?,'owner','name','public',?,'ready',?,?)""", (self.pid, str(self.root), stamp, stamp))
        for side in ('A', 'B'):
            self.db.execute("""INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                              model,image,status,commit_sha,trace_path,attempt_no,error_retry_count,api_retry_count,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?,?,?,'failed',?,?,7,1,1,?,?)""",
                            ('arm-'+side,self.pid,side,side,str(self.root/side),'container-'+side,'screen-'+side,
                             'model','image',side.lower()*40,'trace-'+side,stamp,stamp))
        self.db.execute("INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at) VALUES('delivery-1',?,'discarded',?,?)", (self.pid,stamp,stamp))

    def tearDown(self):
        self.service.executor.shutdown(wait=True, cancel_futures=True)
        self.service.monitor_executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def arm(self, name):
        return self.db.one('SELECT * FROM arm_runs WHERE pair_id=? AND arm=?', (self.pid, name))

    def archive(self, arm, reason, **kwargs):
        self.assertEqual(kwargs['retry_status'], 'manual_preparing')
        self.assertFalse(kwargs['count_error_retry'])
        self.db.execute("""UPDATE arm_runs SET status=?,commit_sha='',trace_path='',session_id='',prompt_id='',
                          prompt_sent_at=NULL,attempt_no=attempt_no+1 WHERE id=?""", (kwargs['retry_status'], arm['id']))
        return self.arm(arm['arm'])

    def test_reset_preserves_history_and_waits_for_explicit_side(self):
        before = self.arm('A')
        result = self.service.reset_pair_retries(self.pid)
        self.assertEqual((result['status'],result['stage'],result['development_failure_count']),('paused','manual_queue',0))
        for a in result['arms']:
            self.assertEqual((a['error_retry_count'],a['api_retry_count'],a['attempt_no']),(0,0,7))
            self.assertEqual(a['status'],'failed')
        self.assertEqual(self.arm('A')['trace_path'],before['trace_path'])
        self.assertEqual(self.arm('A')['commit_sha'],before['commit_sha'])
        self.assertEqual(self.service._waiting_development_arm_count(),0)
        event=self.db.one("SELECT detail_json FROM audit_events WHERE event_type='pair.retry_budget_reset'")
        self.assertEqual(json.loads(event['detail_json'])['previousPair']['development_failure_count'],2)

    def test_reset_does_not_interrupt_active_peer(self):
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?",(self.pid,))
        self.db.execute("UPDATE arm_runs SET status='developing',prompt_sent_at=? WHERE arm='A'",(self.stamp,))
        with patch.object(self.service.claude,'archive_failed_attempt') as archive:
            result=self.service.reset_pair_retries(self.pid)
        archive.assert_not_called()
        self.assertEqual(result['stage'],'development')
        self.assertEqual(self.arm('A')['status'],'developing')
        self.assertEqual(self.arm('A')['prompt_sent_at'],self.stamp)

    def test_submitted_delivery_is_protected(self):
        self.db.execute("UPDATE delivery_submissions SET remote_id='123',status='qc_passed'")
        for action in [lambda:self.service.reset_pair_retries(self.pid),lambda:self.service.queue_arm_manually(self.pid,'B')]:
            with self.assertRaisesRegex(ValueError,'已提交'):action()
        self.assertEqual(self.arm('B')['attempt_no'],7)

    def test_queue_requires_reset_when_budget_exhausted(self):
        with patch.object(self.service.claude,'archive_failed_attempt') as archive:
            with self.assertRaisesRegex(ValueError,'重置失败次数'):self.service.queue_arm_manually(self.pid,'B')
        archive.assert_not_called()

    def test_queue_only_selected_side_and_invalidates_stale_delivery(self):
        self.service.reset_pair_retries(self.pid)
        self.db.execute("UPDATE arm_runs SET status='completed' WHERE arm='A'")
        peer=self.arm('A')
        for side in ('A','B'):
            self.db.execute("INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at) VALUES(?,?,?,?,'passed',?,?)", ('check-'+side,self.pid,side,side.lower()*40,self.stamp,self.stamp))
            self.db.execute("INSERT INTO recordings(id,pair_id,arm,path,status,created_at,updated_at) VALUES(?,?,?,?,'passed',?,?)",('rec-'+side,self.pid,side,'preserved-'+side,self.stamp,self.stamp))
        self.db.execute("INSERT INTO gsb_reviews(id,pair_id,verdict,status,created_at,updated_at) VALUES('g',?,'Same','confirmed',?,?)",(self.pid,self.stamp,self.stamp))
        with patch.object(self.service.claude,'archive_failed_attempt',side_effect=self.archive) as archive, \
             patch.object(self.service.git,'reset_arm_to_baseline') as baseline, \
             patch.object(self.service.claude,'launch') as launch:
            result=self.service.queue_arm_manually(self.pid,'B')
            again=self.service.queue_arm_manually(self.pid,'B')
        self.assertEqual(archive.call_count,1)
        baseline.assert_called_once_with(self.pid,'B');launch.assert_not_called()
        self.assertEqual(self.arm('A'),peer)
        self.assertEqual(self.arm('B')['status'],'waiting_terminal_slot')
        self.assertEqual(self.arm('B')['error_retry_count'],0)
        self.assertEqual(result['stage'],'development');self.assertIsNone(again['gsb'])
        self.assertEqual([x['arm'] for x in result['recordings']],['A'])
        self.assertEqual([x['arm'] for x in result['checks']],['A'])
        self.assertEqual(result['delivery']['status'],'needs_review')
        event=json.loads(self.db.one("SELECT detail_json FROM audit_events WHERE event_type='pair.manual_requeue_snapshot'")['detail_json'])
        self.assertEqual(event['previousRecords']['gsb_reviews'][0]['verdict'],'Same')
        self.assertEqual(self.service._waiting_development_arm_count(),1)
        self.db.set_setting('max_claude_terminals',1)
        self.db.execute("UPDATE arm_runs SET status='developing',prompt_sent_at=? WHERE arm='A'",(self.stamp,))
        self.assertFalse(self.service._reserve_terminal_slot('arm-B'))
        self.assertEqual(self.arm('B')['status'],'waiting_terminal_slot')

    def test_baseline_failure_never_exposes_side_to_scheduler(self):
        self.service.reset_pair_retries(self.pid)
        with patch.object(self.service.claude,'archive_failed_attempt',side_effect=self.archive), \
             patch.object(self.service.git,'reset_arm_to_baseline',side_effect=RuntimeError('git offline')):
            with self.assertRaisesRegex(RuntimeError,'git offline'):self.service.queue_arm_manually(self.pid,'B')
        self.assertEqual(self.arm('B')['status'],'failed')
        self.assertEqual(self.service._waiting_development_arm_count(),0)

    def test_fresh_pair_queue_does_not_implicitly_queue_peer(self):
        self.service.reset_pair_retries(self.pid)
        self.db.execute("UPDATE pairs SET status='queued',stage='ready_to_start' WHERE id=?",(self.pid,))
        self.db.execute("UPDATE arm_runs SET status='queued' WHERE pair_id=?",(self.pid,))
        with patch.object(self.service.claude,'archive_failed_attempt',side_effect=self.archive), \
             patch.object(self.service.git,'reset_arm_to_baseline'):
            self.service.queue_arm_manually(self.pid,'A')
        self.assertEqual(self.arm('B')['status'],'manual_waiting')
        self.assertEqual(self.arm('A')['status'],'waiting_terminal_slot')

    def test_task_detail_is_not_limited_to_first_page(self):
        prompt=('原始题面 <script>不是HTML</script>\n' * 200)+'最后一行'
        self.db.execute("UPDATE tasks SET prompt=? WHERE id='task-1'",(prompt,))
        for n in range(101):
            self.db.execute("INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,fingerprint,status,created_at,updated_at) VALUES(?,'test','zero_to_one','new','new','困难',?,'ready',?,?)",('new-'+str(n),'fp-'+str(n),'2099-01-01','2099-01-01'))
        server=AppServer(('127.0.0.1',0),Handler,self.config,self.db,self.service)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)+'/api/tasks/task-1'
            with urllib.request.urlopen(url) as r:self.assertEqual(json.load(r)['prompt'],prompt)
        finally:server.shutdown();server.server_close();t.join()

if __name__=='__main__':unittest.main()
