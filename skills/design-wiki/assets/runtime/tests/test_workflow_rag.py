from copy import deepcopy
from pathlib import Path
import sys,tempfile,threading,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Workspace,APIError
from workflow import digest,WorkflowError
from rag import citation_check

def fake(m,content='按本次证据判断。[E1]',finish='stop'):
    raw={'id':'TEST','model':'test-double-not-real-api','choices':[{'message':{'content':content},'finish_reason':finish}],'usage':{'prompt_tokens':10,'completion_tokens':10}}
    return dict(content=content,finish_reason=finish,usage=raw['usage'],response_model=raw['model'],response_id='TEST',elapsed_seconds=0,raw=raw)

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.json';self.w=Workspace(self.path);self.w.qa_client=fake
    def tearDown(self):
        for thread in self.w.qa_threads.values():thread.join(5)
        self.tmp.cleanup()
    def rule(self):return self.w._find('rules','WCAG-2.5.8')
    def submit(self,**extra):
        p=dict(rule_id=self.rule()['id'],base_rule_sha256=digest(self.rule()),patch={'min_width':30,'min_height':30},material={'title':'开发构造修订','text':'本例调整当前本地尺寸下限，仅验证维护流程，不改变官方资料及其发布记录。','source_note':'自动测试构造的场景与数值'});p.update(extra)
        return self.w.candidate_submit(p)['candidate']
    def approve(self,c,**extra):
        p=dict(candidate_id=c['id'],decision='approve',reviewer='自动测试（非人工审核）',reason='仅测试本地修改影响与历史保留',evidence='开发构造材料与原始结构字段对照');p.update(extra);return self.w.candidate_review(p)
    def publish(self,c):
        p=self.w.candidate_preview({'candidate_id':c['id']});return self.w.candidate_publish({'candidate_id':c['id'],'preview_id':p['preview_id']})
    def qa(self,**extra):
        p=dict(question='当前本地WCAG-2.5.8目标尺寸是多少？',families=['本地规则'],top_k=3,request_id='test-request-'+str(len(self.w.state['qa_records'])));p.update(extra)
        q=self.w.qa_ask(p)['record'];t=self.w.qa_threads.get(q['id'])
        if t:t.join(5)
        return self.w._find('qa_records',q['id'])
    def test_01_complete_chain(self):
        old=self.w.judge({'case':self.w._find('cases','D01')})['judgment'];other=self.w.judge({'case':self.w._find('cases','D10')})['judgment']
        before=deepcopy(self.rule());c=self.submit();self.assertEqual(before,self.rule())
        with self.assertRaises(WorkflowError):self.publish(c)
        self.approve(c);p=self.publish(c)['publication'];self.assertEqual(self.rule()['min_width'],30)
        self.assertIn(old['id'],p['affected_judgment_ids']);self.assertNotIn(other['id'],p['affected_judgment_ids']);self.assertFalse(p['official_revision'])
        new=self.w.review({'judgment_id':old['id']})['judgment'];self.assertEqual(new['result']['assessment'],'unknown');self.assertEqual(new['parent_id'],old['id'])
        self.assertEqual(self.w._find('judgments',old['id'])['result'],old['result']);self.assertEqual(Workspace(self.path).bootstrap(),self.w.bootstrap())
    def test_02_reject_and_conflict(self):
        c=self.submit();self.approve(c,decision='reject')
        with self.assertRaises(WorkflowError):self.publish(c)
        c=self.submit(conflict_notes=['同一范围的指导尚未裁定'])
        with self.assertRaises(WorkflowError):self.approve(c)
        self.approve(c,conflict_resolution='仅在本地实验适用，保留官方原文，不宣称作废。');self.publish(c)
    def test_03_stale_preview_and_candidate(self):
        c=self.submit();other=self.submit(patch={'min_width':31});self.approve(c);self.approve(other)
        p=self.w.candidate_preview({'candidate_id':c['id']});self.w.judge({'case':self.w._find('cases','D01')})
        with self.assertRaises(WorkflowError):self.w.candidate_publish({'candidate_id':c['id'],'preview_id':p['preview_id']})
        self.publish(c)
        with self.assertRaises(WorkflowError):self.publish(other)
    def test_04_idempotent_publish_immutable_review(self):
        c=self.submit();self.approve(c);self.publish(c);before=self.w.bootstrap()
        self.assertTrue(self.w.candidate_publish({'candidate_id':c['id']})['already_published']);self.assertEqual(before,self.w.bootstrap())
        with self.assertRaises(WorkflowError):self.approve(c)
    def test_05_bad_candidates_cannot_enter(self):
        for extra in ({'reported_qc':'fail'},{'base_rule_sha256':'wrong'},{'patch':{'min_width':-1}},{'patch':{'source':{}}}):
            with self.assertRaises((WorkflowError,APIError)):self.submit(**extra)
        self.assertEqual(len(self.w.state['rule_candidates']),0)
    def test_06_preserve_date_row(self):
        r=self.w.qa_retrieve(dict(question='GOV.UK何时补充开始按钮为何使用链接的解释？',families=['GOV.UK'],source_role='change_log',top_k=3))
        h=next(h for h in r['hits'] if 'Added explanation' in h['text']);self.assertTrue(h['text'].startswith('Oct 2020'));self.assertNotIn('Sept 2019',h['text'])
    def test_07_dual_source_retrieval(self):
        r=self.w.qa_retrieve(dict(question='Ant Design与GOV.UK对主按钮数量有什么建议？',families=['Ant Design','GOV.UK'],source_role='guidance',top_k=4))
        self.assertTrue(any('一个按钮区最多使用一个主按钮' in h['text'] for h in r['hits']));self.assertTrue(any('Avoid using multiple default buttons' in h['text'] for h in r['hits']))
    def test_08_targeted_qa_invalidation_and_reanswer(self):
        local=self.qa();public=self.qa(families=['WCAG'],source_role='normative');answer=local['answer']
        c=self.submit();self.approve(c);p=self.publish(c)['publication'];self.assertIn(local['id'],p['affected_qa_ids']);self.assertNotIn(public['id'],p['affected_qa_ids'])
        self.assertEqual(local['freshness'],'needs_review');self.assertEqual(local['answer'],answer)
        q=self.qa(parent_id=local['id']);self.assertEqual(local['freshness'],'superseded');self.assertEqual(q['parent_id'],local['id'])
    def test_09_response_failures_are_distinct(self):
        for content,finish,status in [('', 'stop','empty'),('半句','length','truncated')]:
            self.w.qa_client=lambda m,c=content,f=finish:fake(m,c,f);self.assertEqual(self.qa()['status'],status)
        def fail(m):raise RuntimeError('测试模拟网络失败')
        self.w.qa_client=fail;self.assertEqual(self.qa()['status'],'error')
    def test_10_no_evidence_and_dedup(self):
        calls=[]
        def counted(m):calls.append(m);return fake(m)
        self.w.qa_client=counted;q=self.qa(question='zzqjvxxqqz',families=['WCAG'],source_role='normative');self.assertEqual(q['status'],'no_evidence');self.assertEqual(calls,[])
        q=self.qa();count=len(calls);self.assertTrue(self.w.qa_ask({'request_id':q['request_id']})['deduplicated']);self.assertEqual(len(calls),count)
    def test_11_citation_identifier_is_not_truth(self):
        r=citation_check('答案[E1][E99]',[{'citation_id':'E1'}]);self.assertEqual(r['invalid_ids'],['E99']);self.assertFalse(r['semantic_support_verified'])
    def test_12_publish_while_model_generates(self):
        entered=threading.Event();release=threading.Event()
        def slow(m):entered.set();release.wait(5);return fake(m)
        self.w.qa_client=slow;q=self.w.qa_ask(dict(question='当前本地WCAG-2.5.8尺寸是多少？',families=['本地规则'],request_id='slow-request'))['record'];self.assertTrue(entered.wait(3))
        try:c=self.submit();self.approve(c);self.publish(c)
        finally:release.set();self.w.qa_threads[q['id']].join(5)
        self.assertEqual(self.w._find('qa_records',q['id'])['freshness'],'needs_review')
    def test_13_changed_material_invalidates_approval(self):
        c=self.submit(registered_document_id='DEMO-TEAM-BUTTON');good=next(x['candidate'] for x in self.w.ingestion_examples()['examples'] if x['id']=='I06');self.w.ingestion_register({'candidate':good})
        with self.assertRaises(WorkflowError):self.approve(c)
    def test_14_restart_does_not_retry_paid_call(self):
        self.w.state['qa_records'].append(dict(id='INT',request_id='INTERRUPTED',status='running',question='测试'));self.w._persist()
        self.assertEqual(Workspace(self.path).state['qa_records'][0]['status'],'interrupted')
    def test_15_failed_call_is_not_an_answer_to_review(self):
        def fail(m):raise RuntimeError('测试模拟网络失败')
        self.w.qa_client=fail;failed=self.qa()
        self.w.qa_client=fake;answered=self.qa()
        c=self.submit();self.approve(c)
        preview=self.w.candidate_preview({'candidate_id':c['id']})
        self.assertEqual([q['id'] for q in preview['qa_items']],[answered['id']])
        pub=self.publish(c)['publication']
        self.assertNotIn(failed['id'],pub['affected_qa_ids'])
        self.assertIn(answered['id'],pub['affected_qa_ids'])
if __name__=='__main__':unittest.main(verbosity=2)
