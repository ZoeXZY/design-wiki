"""Conservative candidate registration checks; no claim of semantic correctness."""
from copy import deepcopy
import hashlib
import json


def fingerprint(document):
    payload = {k: document[k] for k in ('id', 'title', 'version', 'blocks')}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def check_candidate(candidate, baseline):
    if not isinstance(candidate, dict):
        raise ValueError('candidate 必须是对象')
    allowed = {'id', 'title', 'version', 'blocks', 'base_sha256', 'reported_qc'}
    if set(candidate) - allowed:
        raise ValueError('候选版本包含不支持的字段；保护条款与待裁冲突由服务器保存')
    for key in ('id', 'title', 'version', 'base_sha256'):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            raise ValueError(key + ' 必须是非空文字')
    if candidate['id'] != baseline['id']:
        raise ValueError('文档编号与基准不一致')
    blocks = candidate.get('blocks')
    if not isinstance(blocks, list) or len(blocks) > 500:
        raise ValueError('blocks 必须为最多 500 条的数组')
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {'id', 'text'}:
            raise ValueError('每条内容须包含且仅包含 id、text')
        if not all(isinstance(block[k], str) and block[k].strip() for k in ('id', 'text')):
            raise ValueError('内容编号和正文不能为空')
    if candidate.get('reported_qc', 'unknown') not in ('pass', 'fail', 'unknown'):
        raise ValueError('reported_qc 只支持 pass、fail、unknown')
    issues = []

    def add(code, message, ids=None):
        issues.append({'code': code, 'message': message, 'block_ids': ids or []})

    current_hash = fingerprint(baseline)
    if candidate['base_sha256'] != current_hash:
        add('stale_base', '底稿指纹与当前登记版本不一致；不能凭“最新版”名称继续覆盖。')
    if candidate['version'] == baseline['version']:
        add('same_version', '需要新的版本标识，保留旧版本作为历史。')
    ids = [b['id'] for b in blocks]
    if not blocks:
        add('empty_content', '正文为空，不能把零条错误当成检查通过。')
    if len(ids) != len(set(ids)):
        add('duplicate_ids', '条款编号重复，无法可靠定位和比较。')
    old = {b['id']: b['text'] for b in baseline['blocks']}
    new = {b['id']: b['text'] for b in blocks}
    missing = sorted(set(old) - set(new))
    if missing:
        add('missing_blocks', '原有内容缺失；请核对是提取丢失还是经过确认的删除。', missing)
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    protected = sorted(set(changed) & set(baseline['protected_block_ids']))
    if protected:
        add('protected_edit_changed', '已确认的人工修订发生变化，需先复核，不能被重新生成静默覆盖。', protected)
    if candidate.get('reported_qc') == 'fail':
        add('reported_failure', '候选版本带有明确 FAIL 记录，不能登记为当前版本。')
    if baseline.get('open_conflicts'):
        add('unresolved_conflict', '关联规则仍有待裁冲突：' + '；'.join(baseline['open_conflicts']))
    if not baseline.get('source_body_available', False):
        add('source_unavailable', '只有来源记录或链接，未取得正文，不能完成内容核验。')
    return {'can_register': not issues, 'status': 'blocked' if issues else 'ready_to_register',
            'issues': issues, 'base_sha256': current_hash, 'candidate_sha256': fingerprint(candidate),
            'before_count': len(old), 'after_count': len(blocks), 'changed_ids': changed,
            'added_ids': sorted(set(new) - set(old)), 'missing_ids': missing,
            'semantic_verification': 'not_performed',
            'scope': '只检查已登记基准、内容保全及已记录风险；通过表示可登记候选版本，不表示规范生效或语义正确。'}


def demonstration_candidates(documents):
    doc = documents[0]
    good = {k: deepcopy(doc[k]) for k in ('id', 'title', 'version', 'blocks')}
    good.update(version=doc['version'] + '.next', base_sha256=fingerprint(doc), reported_qc='pass')
    good['blocks'].append({'id': 'new-note-' + good['version'], 'text': '新增说明：输出判断时同时保留采用版本与原文出处。'})
    cases = []
    def sample(ident, title, candidate, lesson):
        cases.append({'id': ident, 'title': title, 'candidate': candidate, 'lesson': lesson,
                      'provenance': '根据真实项目台账所述事故机制构造的设计规范测试；不是原附件复现。'})
    stale = deepcopy(good); stale['base_sha256'] = '0' * 64
    sample('I01', '文件叫最新版，底稿却不对', stale, '用内容指纹绑定当前基准。文件名和修改时间都不能代替它。')
    empty = deepcopy(good); empty['blocks'] = empty['blocks'][:1]
    sample('I02', '正文只剩一条，机查却显示通过', empty, '先对照原始条款编号检查完整性，再解释机查结果。')
    rollback = deepcopy(good)
    rollback['blocks'][2]['text'] = '重新生成时移除了已确认的例外与条件。'
    sample('I03', '重新生成覆盖了人工修订', rollback, '保护经人工确认的条款；同样块数也可能发生内容回退。')
    failed = deepcopy(good); failed['reported_qc'] = 'fail'
    sample('I04', '报告写FAIL，仍尝试登记', failed, '拦截发生在服务器登记入口，页面提示不是唯一防线。')
    other = documents[1]
    conflict = {k: deepcopy(other[k]) for k in ('id', 'title', 'version', 'blocks')}
    conflict.update(version=other['version'] + '.next', base_sha256=fingerprint(other), reported_qc='pass')
    sample('I05', '示例与规则的用色冲突仍待裁', conflict, '保留待裁状态，不用较新的时间戳自动决定谁覆盖谁。')
    sample('I06', '保留旧条款，新增可追溯说明', good, '正常对照：登记新版本、保留旧快照，语义核验仍单独说明。')
    return cases
