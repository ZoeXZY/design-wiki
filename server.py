#!/usr/bin/env python3
"""Local design-rule knowledge workspace. Standard-library core with an optional explicit DeepSeek API call."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import unquote, urlparse
from uuid import uuid4
from engine import evaluate
from ingestion import check_candidate, demonstration_candidates, fingerprint
from workflow import WorkflowMixin, WorkflowError
from rag import QAMixin

ROOT = Path(__file__).resolve().parent

def now(): return datetime.now(timezone.utc).isoformat()

def uid(prefix): return prefix + '-' + uuid4().hex[:12]

class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def merge(base, patch):
    result = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict): result[k] = merge(result[k], v)
        else: result[k] = deepcopy(v)
    return result


def changed_paths(before, after, prefix=''):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for k in sorted(set(before) | set(after)):
            p = prefix + '.' + k if prefix else k
            if k not in before or k not in after: result.append(p)
            else: result.extend(changed_paths(before[k], after[k], p))
        return result
    return [] if before == after else [prefix]


def intersects(path, dep): return path == dep or path.startswith(dep + '.') or dep.startswith(path + '.')


def conclusion(result):
    return {k: result.get(k) for k in ('applicability','assessment','relation','joint_lower_bound') if k in result}


def validate_json(value, path='input'):
    if isinstance(value, float) and not math.isfinite(value): raise APIError(path + ' 含非有限数值')
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or k.startswith('__'): raise APIError('非法字段名')
            validate_json(v, path + '.' + k)
    elif isinstance(value, list):
        for x in value: validate_json(x, path)


def validate_case(case, rules):
    if not isinstance(case, dict): raise APIError('case 必须为对象')
    validate_json(case)
    for field in ('id','kind','question'):
        if not isinstance(case.get(field), str) or not case[field].strip(): raise APIError(field + ' 必须为非空字符串')
    if len(case['id']) > 100 or len(case['question']) > 4000: raise APIError('编号或问题过长')
    fields = ('context','measurements','exceptions','comparison_context')
    for f in fields:
        if f in case and not isinstance(case[f], dict): raise APIError(f + ' 必须为对象')
    ruleids = {r['id'] for r in rules}
    if case['kind'] == 'assessment':
        if case.get('rule_id') not in ruleids: raise APIError('未找到所选规则')
        for k,v in case.get('exceptions',{}).items():
            if v is not None and not isinstance(v,bool): raise APIError('例外值必须为 true、false 或 null')
        for k,v in case.get('context',{}).items():
            if k in ('scope','subject','document_version','level') and v is not None and not isinstance(v,str): raise APIError('情境字段须为字符串或 null')
        for k,v in case.get('measurements',{}).items():
            if k in ('width','height','contrast_ratio') and v is not None and (isinstance(v,bool) or not isinstance(v,(int,float))): raise APIError('测量值须为数值或 null')
            if k in ('unit','text_size') and v is not None and not isinstance(v,str): raise APIError('单位和类别须为字符串或 null')
    elif case['kind'] == 'relation':
        if case.get('left_rule') not in ruleids or case.get('right_rule') not in ruleids: raise APIError('关系比较规则不存在')
        for v in case.get('comparison_context',{}).values():
            if v is not None and not isinstance(v,bool): raise APIError('比较条件须为布尔值或 null')
    else: raise APIError('kind 仅支持 assessment 或 relation')

class Workspace(WorkflowMixin,QAMixin):
    def __init__(self, state_path, data_dir=ROOT/'data'):
        self.path = Path(state_path)
        self.lock = threading.RLock()
        self.previews = {}
        data_dir = Path(data_dir)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
        else:
            data_dir = Path(data_dir)
            rules = json.loads((data_dir/'rules.json').read_text(encoding='utf-8'))['rules']
            cases = json.loads((data_dir/'cases.json').read_text(encoding='utf-8'))['cases']
            evidence = json.loads((data_dir/'source_evidence.json').read_text(encoding='utf-8'))
            self.state = {'revision':0,'project':{'name':'设计规范活知识库','author':'ZoeXZY','runtime':'Python 确定性规则与依赖追踪；未调用问答模型'},'rules':rules,'cases':cases,'evidence':evidence['evidence_cards'],'source_documents':evidence['source_documents'],'judgments':[],'changes':[]}
            self._persist()
        self.workflow_init()
        self.qa_init(data_dir)
        if 'ingestion_documents' not in self.state:
            self.state['ingestion_documents'] = json.loads((data_dir/'ingestion_documents.json').read_text(encoding='utf-8'))
            self.state['ingestion_history'] = []
            self._persist()
    def _persist(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.workspace-',suffix='.json',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(self.state,f,ensure_ascii=False,indent=2,allow_nan=False)
                f.flush();os.fsync(f.fileno())
            os.replace(tmp,self.path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    def bootstrap(self):
        with self.lock:return deepcopy(self.state)
    def ingestion_examples(self):
        with self.lock:
            return {'documents':deepcopy(self.state['ingestion_documents']),
                    'examples':demonstration_candidates(self.state['ingestion_documents']),
                    'history_count':len(self.state['ingestion_history'])}
    def ingestion_check(self,payload):
        with self.lock:
            candidate=payload.get('candidate')
            if not isinstance(candidate,dict):raise APIError('candidate 必须为对象')
            validate_json(candidate)
            baseline=self._find('ingestion_documents',candidate.get('id'))
            return check_candidate(candidate,baseline)
    def ingestion_register(self,payload):
        with self.lock:
            # Always recompute against persisted state; never trust a client PASS or old preview.
            result=self.ingestion_check(payload)
            if not result['can_register']:
                raise APIError('登记已拦截：'+'；'.join(x['message'] for x in result['issues']),409)
            candidate=deepcopy(payload['candidate'])
            baseline=self._find('ingestion_documents',candidate['id'])
            old=deepcopy(baseline)
            for key in ('title','version','blocks'):baseline[key]=candidate[key]
            baseline['registration_status']='candidate_registered_not_semantically_verified'
            event={'id':uid('I'),'created_at':now(),'document_id':candidate['id'],
                   'before':old,'after':deepcopy(baseline),'inspection':result,
                   'submitted_candidate':candidate,'previous_sha256':fingerprint(old),
                   'current_sha256':fingerprint(baseline)}
            self.state['ingestion_history'].append(event)
            self.state['revision']+=1;self._persist()
            return {'record':deepcopy(event),'inspection':result}
    def _find(self,kind,ident):
        for x in self.state[kind]:
            if x['id']==ident:return x
        raise APIError('未找到 '+kind+'：'+str(ident),404)
    def _record(self,case,parent_id=None):
        result=evaluate(case,self.state['rules'])
        used={result['rule_id']} if 'rule_id' in result else set(result['rule_ids'])
        rec={'id':uid('J'),'case_id':case['id'],'created_at':now(),'result':result,'status':'current','case_snapshot':deepcopy(case),'rule_snapshots':[deepcopy(r) for r in self.state['rules'] if r['id'] in used],'parent_id':parent_id,'revision':self.state['revision']}
        return rec
    def judge(self,payload):
        with self.lock:
            case=deepcopy(payload.get('case'))
            validate_case(case,self.state['rules'])
            save=payload.get('save',True)
            if not isinstance(save,bool):raise APIError('save 须为布尔值')
            existing=next((x for x in self.state['cases'] if x['id']==case['id']),None)
            history=[j for j in self.state['judgments'] if j['case_id']==case['id']]
            if existing is not None and existing!=case and history:
                raise APIError('已有历史判断。请先预览并提交情境变化，再复核。',409)
            pending=[j for j in history if j['status']=='needs_review']
            if pending and save:raise APIError('该案例有待复核判断，请使用复核操作保留版本关系。',409)
            rec=self._record(case)
            if save:
                if existing is None:self.state['cases'].append(case)
                elif existing!=case:existing.clear();existing.update(case)
                # Repeating a judgment does not overwrite prior evidence.
                current=next((j for j in reversed(history) if j['status']=='current'),None)
                if current:
                    current['status']='superseded';rec['parent_id']=current['id']
                self.state['judgments'].append(rec)
                self.state['revision']+=1;self._persist()
            return {'judgment':deepcopy(rec)}
    def preview(self,payload):
        with self.lock:
            typ=payload.get('type');target_id=payload.get('target_id');patch=payload.get('patch')
            if typ not in ('context','rule') or not isinstance(target_id,str) or not isinstance(patch,dict) or not patch:raise APIError('须提供 type、target_id 和非空 patch')
            validate_json(patch)
            if typ=='context':
                if set(patch)-{'context','measurements','exceptions','comparison_context','question','project_name','rule_id','left_rule','right_rule'}:raise APIError('情境 patch 含不允许修改的字段')
                target=self._find('cases',target_id);updated=merge(target,patch);validate_case(updated,self.state['rules'])
            else:
                if set(patch)-{'title','summary','min_width','min_height','thresholds'}:raise APIError('规则仅允许修改标题、摘要和数值约束；原来源保持不变')
                target=self._find('rules',target_id)
                for k,v in patch.items():
                    if k in ('title','summary') and (not isinstance(v,str) or not v.strip()):raise APIError('标题和摘要须为非空文字')
                    if k in ('min_width','min_height') and (target['kind']!='minimum_rectangle' or isinstance(v,bool) or not isinstance(v,(int,float)) or v<=0):raise APIError('尺寸阈值须为正数且属于尺寸规则')
                    if k=='thresholds':
                        if target['kind']!='minimum_contrast' or not isinstance(v,dict) or not v or set(v)-{'normal','large'}:raise APIError('对比度阈值只支持 normal 与 large')
                        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or x<1 or x>21 for x in v.values()):raise APIError('对比度阈值须在1至21之间')
                updated=merge(target,patch)
            fields=changed_paths(target,updated)
            if not fields:raise APIError('没有实际变化')
            preview_rules=[updated if typ=='rule' and r['id']==target_id else r for r in self.state['rules']]
            items=[]
            for j in self.state['judgments']:
                if j['status']=='superseded':continue
                dep=j['result']['dependencies']
                relevant=j['case_id']==target_id if typ=='context' else 'rule:'+target_id in dep
                if not relevant:continue
                case=updated if typ=='context' else self._find('cases',j['case_id'])
                after=evaluate(case,preview_rules)
                hits=[f for f in fields if typ=='rule' or any(intersects(f,d) for d in dep)]
                needs=bool(hits) or j['status']=='needs_review'
                items.append({'judgment_id':j['id'],'case_id':j['case_id'],'needs_review':needs,'conclusion_changed':conclusion(j['result'])!=conclusion(after),'before':deepcopy(j['result']),'after':after,'reasons':(['依赖字段发生变化：'+', '.join(hits)] if hits else ['变化未命中已保存判断依赖'])+(['该记录原已待复核'] if j['status']=='needs_review' else [])})
            p={'preview_id':uid('P'),'type':typ,'target_id':target_id,'label':str(payload.get('label',''))[:300],'created_at':now(),'base_revision':self.state['revision'],'changed_fields':fields,'patch':deepcopy(patch),'before_target':deepcopy(target),'after_target':updated,'items':items,'summary':{'total':len(items),'needs_review':sum(i['needs_review'] for i in items),'conclusion_changed':sum(i['conclusion_changed'] for i in items)}}
            self.previews[p['preview_id']]=p
            return deepcopy(p)
    def apply(self,payload,*,approved_candidate=None,persist=True):
        with self.lock:
            ident=payload.get('preview_id')
            if not isinstance(ident,str) or ident not in self.previews:raise APIError('预览不存在，请重新预览',404)
            p=self.previews[ident]
            if p['type']=='rule':
                if approved_candidate is None or not any(c is approved_candidate for c in self.state['rule_candidates']):
                    raise APIError('规则更新须先提交候选、人工审核，再从发布预览提交。',409)
                if approved_candidate['status']!='approved' or p.get('candidate_id')!=approved_candidate['id']:
                    raise APIError('发布预览与审核记录不一致。',409)
            if p['base_revision']!=self.state['revision']:raise APIError('状态已变化，旧预览不能提交，请重新预览',409)
            updated=deepcopy(p['after_target'])
            if p['type']=='rule':
                updated['local_override']={'kind':'local_experiment','label':p['label'] or '本地实验覆盖','changed_at':now(),'official_revision':False,'base_rule_id':p['target_id'],'candidate_id':approved_candidate['id'],'reviewer':approved_candidate['review']['reviewer']}
            target=self._find('cases' if p['type']=='context' else 'rules',p['target_id']);target.clear();target.update(updated)
            for item in p['items']:
                if item['needs_review']:self._find('judgments',item['judgment_id'])['status']='needs_review'
            event=deepcopy(p);event['id']=uid('C');event['applied_at']=now();event['after_target']=deepcopy(updated)
            if p['type']=='rule':event['source_notice']='本地实验修改，不是 W3C 官方修订。'
            self.state['changes'].append(event);self.state['revision']+=1
            if persist:self._persist()
            del self.previews[ident]
            return {'change':deepcopy(event)}
    def review(self,payload):
        with self.lock:
            ident=payload.get('judgment_id')
            if not isinstance(ident,str):raise APIError('须提供 judgment_id')
            old=self._find('judgments',ident)
            if old['status']!='needs_review':raise APIError('仅待复核记录可以建立复核版本',409)
            case=self._find('cases',old['case_id']);new=self._record(case,old['id'])
            new['review_note']='根据当前条款和情境重新计算；历史快照保持不变。'
            old['status']='superseded';self.state['judgments'].append(new);self.state['revision']+=1;self._persist()
            return {'judgment':deepcopy(new)}


def make_server(workspace,port=8765,static_dir=ROOT/'static'):
    static_dir=Path(static_dir).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_json(self,value,status=200,attachment=False):
            raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
            if attachment:self.send_header('Content-Disposition','attachment; filename="design-wiki-export.json"')
            self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            path=unquote(urlparse(self.path).path)
            if path=='/api/bootstrap':return self.send_json(workspace.bootstrap())
            if path=='/api/export':return self.send_json(workspace.bootstrap(),attachment=True)
            if path=='/api/ingestion/examples':return self.send_json(workspace.ingestion_examples())
            if path=='/api/workflow':return self.send_json(workspace.workflow_state())
            if path=='/api/qa/config':return self.send_json(workspace.qa_config())
            if path=='/api/qa/history':return self.send_json(workspace.qa_history())
            if path.startswith('/api/'):return self.send_json({'error':'接口不存在'},404)
            file=(static_dir/('index.html' if path=='/' else path.lstrip('/'))).resolve()
            if not file.is_relative_to(static_dir):return self.send_json({'error':'禁止访问静态目录之外的路径'},403)
            if not file.is_file():return self.send_json({'error':'文件不存在'},404)
            raw=file.read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(len(raw)));self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            try:
                origin=self.headers.get('Origin')
                if origin and origin not in ('http://127.0.0.1:'+str(self.server.server_port),'http://localhost:'+str(self.server.server_port)):raise APIError('只接受本地页面发起的修改',403)
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:raise APIError('无效 Content-Length')
                if size<=0 or size>1024*1024:raise APIError('请求大小须为1字节至1MB',413)
                if 'application/json' not in self.headers.get('Content-Type',''):raise APIError('须发送 application/json',415)
                try:
                    data=json.loads(self.rfile.read(size).decode('utf-8'),parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
                except (ValueError,UnicodeDecodeError):raise APIError('JSON 格式不正确')
                if not isinstance(data,dict):raise APIError('请求须为 JSON 对象')
                methods={'/api/judge':workspace.judge,'/api/preview':workspace.preview,'/api/apply':workspace.apply,'/api/review':workspace.review,'/api/ingestion/check':workspace.ingestion_check,'/api/ingestion/register':workspace.ingestion_register}
                methods.update({'/api/candidates/submit':workspace.candidate_submit,'/api/candidates/review':workspace.candidate_review,'/api/candidates/preview':workspace.candidate_preview,'/api/candidates/publish':workspace.candidate_publish,'/api/qa/retrieve':workspace.qa_retrieve,'/api/qa/ask':workspace.qa_ask})
                path=urlparse(self.path).path
                if path not in methods:raise APIError('接口不存在',404)
                self.send_json(methods[path](data))
            except APIError as e:self.send_json({'error':str(e)},e.status)
            except WorkflowError as e:self.send_json({'error':str(e)},e.status)
            except (KeyError,TypeError,ValueError) as e:self.send_json({'error':'数据不能处理：'+str(e)},400)
            except Exception:self.send_json({'error':'内部处理失败，原记录未被主动覆盖，请查看服务端。'},500)
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--state',type=Path,default=ROOT/'runtime/workspace.json')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    args=parser.parse_args()
    server=make_server(Workspace(args.state,args.data_dir),args.port)
    print('设计规范活知识库 http://127.0.0.1:'+str(server.server_port),flush=True)
    print('本地状态文件：'+str(args.state.resolve()),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
