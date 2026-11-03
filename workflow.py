"""Review, publish and invalidate dependent records without rewriting history."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4


def stamp(): return datetime.now(timezone.utc).isoformat()
def ident(prefix): return prefix + '-' + uuid4().hex[:12]
def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def qa_needs_review(record,rule_id):
    return (rule_id in record.get('rule_dependencies',{})
            and record.get('freshness')!='superseded'
            and record.get('status') in ('queued','running','success','truncated'))


class WorkflowError(ValueError):
    def __init__(self,message,status=400):
        super().__init__(message); self.status=status


def require_text(value,label,minimum=1,maximum=12000):
    if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum:
        raise WorkflowError(f'{label}须为{minimum}至{maximum}字的文字')
    return value.strip()


class WorkflowMixin:
    def workflow_init(self):
        changed=False
        for key in ('rule_candidates','rule_publications','qa_records'):
            if key not in self.state:self.state[key]=[];changed=True
        for record in self.state['qa_records']:
            if record['status'] in ('queued','running'):
                record.update(status='interrupted',error='服务重启；不自动重试，原请求可能已计费。');changed=True
        if changed:self._persist()

    def workflow_state(self):
        with self.lock:
            return {'rules':[{'rule':deepcopy(r),'sha256':digest(r)} for r in self.state['rules']],
                    'candidates':deepcopy(self.state['rule_candidates']),
                    'publications':deepcopy(self.state['rule_publications']),
                    'documents':deepcopy(self.state['ingestion_documents'])}

    def candidate_submit(self,payload):
        with self.lock:
            rule_id=require_text(payload.get('rule_id'),'规则编号')
            rule=self._find('rules',rule_id)
            if payload.get('base_rule_sha256')!=digest(rule):raise WorkflowError('规则基准已变化，请刷新后重新提交。',409)
            material=payload.get('material')
            if not isinstance(material,dict):raise WorkflowError('需要来源材料对象')
            material={'title':require_text(material.get('title'),'材料标题',1,200),
                      'text':require_text(material.get('text'),'材料正文',20,16000),
                      'source_note':require_text(material.get('source_note'),'材料来源说明',5,1500)}
            linked=payload.get('registered_document_id')
            linked_snapshot=None
            if linked:
                linked_snapshot=deepcopy(self._find('ingestion_documents',linked))
                if not linked_snapshot.get('source_body_available'):raise WorkflowError('关联材料只有链接，没有正文。')
            # Reuse the same strict patch validation as the existing rule engine.
            preview=self.preview({'type':'rule','target_id':rule_id,'patch':payload.get('patch'),'label':'候选版本结构检查'})
            self.previews.pop(preview['preview_id'],None)
            if payload.get('reported_qc','unknown') not in ('pass','unknown','fail'):raise WorkflowError('无效机查状态')
            if payload.get('reported_qc')=='fail':raise WorkflowError('材料带有明确FAIL，请修复后再提交。',409)
            conflicts=payload.get('conflict_notes',[])
            if not isinstance(conflicts,list) or len(conflicts)>20:raise WorkflowError('冲突列表最多20条')
            conflicts=[require_text(x,'冲突说明',1,1000) for x in conflicts]
            conflicts+=deepcopy((linked_snapshot or {}).get('open_conflicts',[]))
            c={'id':ident('CAND'),'created_at':stamp(),'status':'pending_review','rule_id':rule_id,
               'patch':deepcopy(payload['patch']),'base_rule_sha256':digest(rule),
               'before_rule':deepcopy(rule),'proposed_rule':preview['after_target'],
               'material':material,'material_sha256':digest(material),'linked_document':linked_snapshot,
               'conflict_notes':conflicts,'reported_qc':payload.get('reported_qc','unknown'),
               'scope':'local_experiment','source_notice':'本地项目规则修订，不能代表W3C官方修订。',
               'inspection':{'structural_validation':'passed','semantic_validation':'pending_human_review',
                             'changed_fields':preview['changed_fields']},'review':None}
            c['candidate_sha256']=digest({k:c[k] for k in ('rule_id','patch','base_rule_sha256','material_sha256','linked_document','conflict_notes')})
            self.state['rule_candidates'].append(c);self.state['revision']+=1;self._persist()
            return {'candidate':deepcopy(c)}

    def _check_candidate_base(self,c):
        if digest(self._find('rules',c['rule_id']))!=c['base_rule_sha256']:
            raise WorkflowError('该候选基于旧规则；请根据当前规则重新提交并审核。',409)
        if c.get('linked_document'):
            current=self._find('ingestion_documents',c['linked_document']['id'])
            if digest(current)!=digest(c['linked_document']):raise WorkflowError('关联材料已变化，原候选需要重新提交。',409)

    def candidate_review(self,payload):
        with self.lock:
            c=self._find('rule_candidates',payload.get('candidate_id'))
            if c['status']!='pending_review':raise WorkflowError('只有待审核候选可以裁决；裁决记录不覆盖。',409)
            decision=payload.get('decision')
            if decision not in ('approve','reject'):raise WorkflowError('请选择批准或退回')
            reviewer=require_text(payload.get('reviewer'),'复核人',1,100)
            reason=require_text(payload.get('reason'),'裁决理由',8,4000)
            evidence=require_text(payload.get('evidence'),'核查依据',8,4000)
            resolution=str(payload.get('conflict_resolution','')).strip()
            if decision=='approve':
                self._check_candidate_base(c)
                if c['conflict_notes'] and len(resolution)<8:raise WorkflowError('请写明冲突如何裁定，不能只选择批准。')
            c['review']={'reviewer':reviewer,'decision':decision,'reason':reason,'evidence':evidence,
                         'conflict_resolution':resolution,'reviewed_at':stamp(),
                         'candidate_sha256':c['candidate_sha256'],
                         'identity_note':'用户在本地填写；未接入身份认证，不代表原规范发布方批准。'}
            c['status']='approved' if decision=='approve' else 'rejected'
            self.state['revision']+=1;self._persist();return {'candidate':deepcopy(c)}

    def candidate_preview(self,payload):
        with self.lock:
            c=self._find('rule_candidates',payload.get('candidate_id'))
            if c['status']!='approved':raise WorkflowError('先完成明确的人工批准，再预览发布。',409)
            self._check_candidate_base(c)
            p=self.preview({'type':'rule','target_id':c['rule_id'],'patch':c['patch'],'label':'经审核的本地规则发布'})
            p['candidate_id']=c['id'];p['candidate_sha256']=c['candidate_sha256']
            p['qa_items']=[{'id':q['id'],'question':q['question'],'status':q.get('freshness','current')}
                           for q in self.state['qa_records'] if qa_needs_review(q,c['rule_id'])]
            self.previews[p['preview_id']]=deepcopy(p)
            return p

    def candidate_publish(self,payload):
        with self.lock:
            c=self._find('rule_candidates',payload.get('candidate_id'))
            if c['status']=='published':return {'publication':deepcopy(next(p for p in self.state['rule_publications'] if p['candidate_id']==c['id'])),'already_published':True}
            if c['status']!='approved':raise WorkflowError('未批准的候选不能发布。',409)
            self._check_candidate_base(c)
            preview=self.previews.get(payload.get('preview_id'))
            if not preview or preview.get('candidate_id')!=c['id'] or preview.get('candidate_sha256')!=c['candidate_sha256']:
                raise WorkflowError('需要此候选的最新发布预览。',409)
            if preview['base_revision']!=self.state['revision']:raise WorkflowError('预览后状态变化，请重新预览。',409)
            before=deepcopy(self.state)
            try:
                applied=self.apply({'preview_id':preview['preview_id']},approved_candidate=c,persist=False)['change']
                affected=[]
                for q in self.state['qa_records']:
                    if qa_needs_review(q,c['rule_id']):
                        q['freshness']='needs_review';q.setdefault('change_reasons',[]).append(c['id']);affected.append(q['id'])
                publication={'id':ident('PUB'),'candidate_id':c['id'],'published_at':stamp(),
                             'rule_id':c['rule_id'],'review':deepcopy(c['review']),
                             'before_rule':deepcopy(c['before_rule']),'after_rule':deepcopy(self._find('rules',c['rule_id'])),
                             'affected_judgment_ids':[i['judgment_id'] for i in applied['items'] if i['needs_review']],
                             'affected_qa_ids':affected,'change_id':applied['id'],'official_revision':False}
                c['status']='published';c['publication_id']=publication['id']
                self.state['rule_publications'].append(publication);self._persist()
            except Exception:
                self.state=before;self.previews[preview['preview_id']]=preview;raise
            return {'publication':deepcopy(publication),'already_published':False}
