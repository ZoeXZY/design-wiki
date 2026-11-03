import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode=True
from server import Workspace,APIError,make_server
from engine import evaluate
from workflow import digest

class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.state=Path(self.tmp.name)/'state.json';self.w=Workspace(self.state)
    def tearDown(self):self.tmp.cleanup()
    def case(self,id):return next(x for x in self.w.bootstrap()['cases'] if x['id']==id)
    def save(self,id):return self.w.judge({'case':self.case(id),'save':True})['judgment']
    def change(self,typ,target,patch):return self.w.preview({'type':typ,'target_id':target,'patch':patch,'label':'自动测试构造变更'})
    def test_01_seed_cases_match_development_reference(self):
        raw=json.loads((ROOT/'data/expected.json').read_text())
        expected=raw.get('judgments',[]) + raw.get('relations',[])
        if isinstance(expected,dict):entries=[dict(id=k,**v) for k,v in expected.items()]
        else:entries=expected
        self.assertEqual(len(entries),20)
        for e in entries:
            actual=evaluate(self.case(e['id']),self.w.bootstrap()['rules'])
            for key in ('applicability','assessment','relation'):
                if key in e:self.assertEqual(actual[key],e[key],e['id'])
    def test_02_initial_judgment_immutable_snapshot(self):
        j=self.save('D06');self.assertEqual(j['result']['assessment'],'not_required')
        self.assertTrue(j['case_snapshot']);self.assertTrue(j['rule_snapshots'])
        self.assertIn('context.level',j['result']['dependencies'])
    def test_03_relevant_context_preview_apply_review(self):
        old=self.save('D06');before=self.w.bootstrap();p=self.change('context','D06',{'context':{'level':'AAA'}})
        self.assertEqual(self.w.bootstrap(),before)
        self.assertTrue(p['items'][0]['needs_review']);self.assertTrue(p['items'][0]['conclusion_changed'])
        self.w.apply({'preview_id':p['preview_id']})
        self.assertEqual(self.w.bootstrap()['judgments'][0]['status'],'needs_review')
        new=self.w.review({'judgment_id':old['id']})['judgment']
        self.assertEqual(new['result']['assessment'],'fail');self.assertEqual(new['parent_id'],old['id'])
        historical=self.w.bootstrap()['judgments'][0]
        self.assertEqual(historical['result'],old['result']);self.assertEqual(historical['case_snapshot'],old['case_snapshot']);self.assertEqual(historical['status'],'superseded')
    def test_04_unrelated_context_has_no_review(self):
        j=self.save('D06');p=self.change('context','D06',{'project_name':'另一显示名称'})
        self.assertEqual(p['summary']['needs_review'],0);self.assertFalse(p['items'][0]['conclusion_changed'])
        self.w.apply({'preview_id':p['preview_id']});self.assertEqual(self.w.bootstrap()['judgments'][0]['status'],'current')
        with self.assertRaises(APIError):self.w.review({'judgment_id':j['id']})
    def test_05_local_rule_revision_preserves_official_and_history(self):
        old=self.save('D01');p=self.change('rule','WCAG-2.5.8',{'min_width':30})
        self.assertTrue(p['items'][0]['needs_review'])
        with self.assertRaises(APIError):self.w.apply({'preview_id':p['preview_id']})
        rule=next(r for r in self.w.bootstrap()['rules'] if r['id']=='WCAG-2.5.8')
        c=self.w.candidate_submit({'rule_id':rule['id'],'base_rule_sha256':digest(rule),'patch':{'min_width':30},'material':{'title':'测试修订','text':'这是自动回归测试构造的本地规则调整，不代表真实用户审核或官方修订。','source_note':'测试用结构化依据'}})['candidate']
        self.w.candidate_review({'candidate_id':c['id'],'decision':'approve','reviewer':'自动测试','reason':'验证本地规则发布后的历史保留','evidence':'开发构造案例与输入数值对照'})
        preview=self.w.candidate_preview({'candidate_id':c['id']})
        self.w.candidate_publish({'candidate_id':c['id'],'preview_id':preview['preview_id']})
        event=self.w.bootstrap()['changes'][-1]
        updated=next(r for r in self.w.bootstrap()['rules'] if r['id']=='WCAG-2.5.8')
        self.assertFalse(updated['local_override']['official_revision']);self.assertIn('不是 W3C',event['source_notice'])
        self.assertEqual(updated['source'],old['rule_snapshots'][0]['source'])
        self.assertEqual(self.w.bootstrap()['judgments'][0]['rule_snapshots'][0]['min_width'],24)
        new=self.w.review({'judgment_id':old['id']})['judgment'];self.assertEqual(new['rule_snapshots'][0]['min_width'],30)
    def test_06_rule_wording_change_requires_review_without_conclusion_change(self):
        self.save('D01');p=self.change('rule','WCAG-2.5.8',{'summary':'本地措辞调整，数值不变'})
        self.assertTrue(p['items'][0]['needs_review']);self.assertFalse(p['items'][0]['conclusion_changed'])
    def test_07_stale_preview_rejected(self):
        self.save('D06');p=self.change('context','D06',{'context':{'level':'AAA'}});self.save('D01')
        with self.assertRaises(APIError) as cm:self.w.apply({'preview_id':p['preview_id']})
        self.assertEqual(cm.exception.status,409);self.assertEqual(self.case('D06')['context']['level'],'AA')
    def test_08_persistence_restart(self):
        self.save('D06');p=self.change('context','D06',{'context':{'level':'AAA'}});self.w.apply({'preview_id':p['preview_id']})
        self.assertEqual(Workspace(self.state).bootstrap(),self.w.bootstrap())
    def test_09_guard_against_silent_context_overwrite(self):
        old=self.save('D06');c=self.case('D06');c['context']['level']='AAA'
        with self.assertRaises(APIError):self.w.judge({'case':c,'save':True})
        self.assertEqual(self.w.bootstrap()['judgments'][0],old)
    def test_10_invalid_inputs(self):
        for patch in ({'id':'bad'},{'min_width':-1},{'min_width':True},{'thresholds':{'normal':4}},{'source':{'urls':['evil']}}):
            with self.assertRaises(APIError):self.change('rule','WCAG-2.5.8',patch)
        c=self.case('D01');c['exceptions']['inline']='yes'
        with self.assertRaises(APIError):self.w.judge({'case':c})
        with self.assertRaises(APIError):self.w.judge({'case':None})
        c=self.case('D10');c['measurements']['contrast_ratio']=22
        self.assertEqual(self.w.judge({'case':c,'save':False})['judgment']['result']['assessment'],'unknown')
    def test_11_superseded_history_not_reused(self):
        old=self.save('D01');new=self.save('D01')
        self.assertEqual(new['parent_id'],old['id']);self.assertEqual(self.w.bootstrap()['judgments'][0]['status'],'superseded')
        p=self.change('rule','WCAG-2.5.8',{'min_width':28});self.assertEqual(len(p['items']),1)
    def test_12_relation_judgment_supported(self):
        j=self.save('R01');self.assertEqual(j['result']['relation'],'兼容')
        p=self.change('context','R01',{'comparison_context':{'same_object':False}})
        self.assertTrue(p['items'][0]['needs_review']);self.assertEqual(p['items'][0]['after']['relation'],'不可比')

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);(p/'static').mkdir();(p/'static/index.html').write_text('<h1>test</h1>');(p/'secret.txt').write_text('secret')
        self.server=make_server(Workspace(p/'state.json'),0,p/'static');self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def req(self,path,data=None,raw=None,headers=None):
        h={'Content-Type':'application/json'};h.update(headers or {})
        r=Request('http://127.0.0.1:'+str(self.port)+path,data=raw if raw is not None else json.dumps(data).encode() if data is not None else None,headers=h)
        try:
            with urlopen(r) as x:return x.status,x.read(),x.headers
        except HTTPError as x:return x.code,x.read(),x.headers
    def test_13_bootstrap_and_export(self):
        code,raw,_=self.req('/api/bootstrap');d=json.loads(raw)
        self.assertEqual(code,200);self.assertEqual(len(d['rules']),3);self.assertEqual(len(d['cases']),20)
        code,raw,h=self.req('/api/export');self.assertEqual(code,200);self.assertIn('attachment',h['Content-Disposition'])
    def test_14_path_traversal(self):
        code,raw,_=self.req('/%2e%2e/secret.txt');self.assertEqual(code,403);self.assertNotIn(b'secret',raw)
    def test_15_bad_json_and_origin(self):
        self.assertEqual(self.req('/api/judge',raw=b'{broken')[0],400)
        self.assertEqual(self.req('/api/judge',raw=b'[]')[0],400)
        self.assertEqual(self.req('/api/judge',data={},headers={'Origin':'https://example.com'})[0],403)
    def test_16_end_to_end_api(self):
        _,raw,_=self.req('/api/bootstrap');c=next(c for c in json.loads(raw)['cases'] if c['id']=='D06')
        code,raw,_=self.req('/api/judge',{'case':c,'save':True});j=json.loads(raw)['judgment'];self.assertEqual(code,200)
        code,raw,_=self.req('/api/preview',{'type':'context','target_id':'D06','patch':{'context':{'level':'AAA'}}});pr=json.loads(raw);self.assertTrue(pr['items'][0]['needs_review'])
        self.assertEqual(self.req('/api/apply',{'preview_id':pr['preview_id']})[0],200)
        code,raw,_=self.req('/api/review',{'judgment_id':j['id']});self.assertEqual(json.loads(raw)['judgment']['result']['assessment'],'fail')

if __name__=='__main__':unittest.main(verbosity=2)
