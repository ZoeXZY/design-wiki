#!/usr/bin/env python3
"""Portable CLI for the bundled design-wiki service. Python 3.10+."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'assets/runtime'
POST = {
    'submit': '/api/candidates/submit',
    'decide': '/api/candidates/review',
    'judge': '/api/judge',
    'context-preview': '/api/preview',
    'context-apply': '/api/apply',
    'check-material': '/api/ingestion/check',
    'register-material': '/api/ingestion/register',
}


def read_object(path):
    text = sys.stdin.read() if path == '-' else Path(path).read_text(encoding='utf-8')
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError('输入必须是JSON对象。')
    return obj


def local_url(value):
    u = urlsplit(value)
    if (u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost')
            or u.username or u.password or u.query or u.fragment or u.path not in ('', '/')):
        raise argparse.ArgumentTypeError('只支持本机HTTP服务，如http://127.0.0.1:8786。')
    try:
        port = u.port
    except ValueError as error:
        raise argparse.ArgumentTypeError('端口无效。') from error
    if port is None:
        raise argparse.ArgumentTypeError('请明确填写本地端口。')
    return value.rstrip('/')


def request(base, endpoint, data=None):
    req = Request(base + endpoint, data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
                  headers={'Content-Type': 'application/json', 'Origin': base})
    try:
        with urlopen(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            detail = json.loads(error.read()).get('error', '接口拒绝请求')
        except (ValueError, AttributeError):
            detail = '接口拒绝请求'
        raise RuntimeError(f'HTTP {error.code}: {detail}') from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('无法取得本地服务响应。请确认服务；若刚提交问答，先按request_id查历史，勿直接重试。') from None


def emit(value, output=None):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if output:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    print(text, end='')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', type=local_url, default='http://127.0.0.1:8786')
    sub = p.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='启动随包服务，数据写入指定工作区')
    serve.add_argument('--workspace', type=Path, required=True)
    serve.add_argument('--port', type=int, default=8786)
    for command in ('status', 'rules', 'candidates', 'history', 'export'):
        sub.add_parser(command).add_argument('--output')
    for command in ('retrieve', 'ask'):
        q = sub.add_parser(command)
        q.add_argument('--question', required=True)
        q.add_argument('--family', action='append', default=[])
        q.add_argument('--role', choices=['', 'normative', 'guidance', 'change_log', 'local_policy'], default='')
        q.add_argument('--top-k', type=int, default=4)
        q.add_argument('--output')
        if command == 'ask':
            q.add_argument('--execute-api', action='store_true', help='实际发送付费请求；使用已有用户授权')
            q.add_argument('--request-id')
            q.add_argument('--parent-id')
    wait = sub.add_parser('wait', help='仅查询已有任务，不发起模型请求')
    wait.add_argument('--id', required=True)
    wait.add_argument('--timeout', type=float, default=30)
    wait.add_argument('--output')
    for command in POST:
        c = sub.add_parser(command)
        c.add_argument('--input', required=True, help='JSON文件路径；-表示标准输入')
        c.add_argument('--output')
    for command in ('preview', 'publish', 'review-judgment'):
        c = sub.add_parser(command)
        c.add_argument('--id', required=True, help='候选或判断编号')
        c.add_argument('--output')
        if command == 'publish':
            c.add_argument('--preview-id', required=True)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        command = args.command
        if command == 'serve':
            if not 1 <= args.port <= 65535:
                raise ValueError('端口须为1至65535。')
            workspace = args.workspace.expanduser().resolve()
            if workspace == ROOT or ROOT in workspace.parents:
                raise ValueError('运行数据须放在Skill目录以外，避免更新Skill时覆盖历史。')
            workspace.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            return subprocess.call([sys.executable, '-B', str(RUNTIME/'server.py'), '--port', str(args.port),
                                    '--state', str(workspace/'workspace.json')], cwd=workspace, env=env)
        if command == 'status':
            config = request(args.url, '/api/qa/config')
            wf = request(args.url, '/api/workflow')
            result = {'url': args.url, 'configuration': config, 'rules': len(wf['rules']),
                      'candidates': len(wf['candidates']), 'publications': len(wf['publications'])}
        elif command in ('rules', 'candidates'):
            result = request(args.url, '/api/workflow')[command]
        elif command == 'history':
            result = request(args.url, '/api/qa/history')
        elif command == 'export':
            result = request(args.url, '/api/export')
        elif command in ('retrieve', 'ask'):
            payload = {'question': args.question, 'families': args.family, 'source_role': args.role, 'top_k': args.top_k}
            endpoint = '/api/qa/retrieve'
            if command == 'ask':
                if not args.execute_api:
                    raise ValueError('ask会实际调用付费模型；已有授权时添加--execute-api。只查资料请用retrieve。')
                request_id = args.request_id or ('skill-' + uuid4().hex)
                print('request_id=' + request_id, file=sys.stderr, flush=True)
                payload.update(request_id=request_id, parent_id=args.parent_id)
                endpoint = '/api/qa/ask'
            result = request(args.url, endpoint, payload)
        elif command == 'wait':
            if not 0 <= args.timeout <= 50:
                raise ValueError('单次等待限0至50秒；未完成可继续wait，不重新ask。')
            deadline = time.monotonic() + args.timeout
            while True:
                records = request(args.url, '/api/qa/history')['records']
                result = next((q for q in records if q['id'] == args.id), None)
                if result is None:
                    raise ValueError('问答编号不存在。')
                if result['status'] not in ('queued', 'running') or time.monotonic() >= deadline:
                    break
                time.sleep(min(1, max(0, deadline-time.monotonic())))
            emit(result, args.output)
            return 3 if result['status'] in ('queued', 'running') else 0 if result['status'] == 'success' else 1
        elif command in POST:
            payload = read_object(args.input)
            if command == 'context-preview' and payload.get('type') != 'context':
                raise ValueError('规则修订必须使用submit、decide、preview、publish流程。')
            result = request(args.url, POST[command], payload)
        elif command == 'preview':
            result = request(args.url, '/api/candidates/preview', {'candidate_id': args.id})
        elif command == 'publish':
            result = request(args.url, '/api/candidates/publish', {'candidate_id': args.id, 'preview_id': args.preview_id})
        elif command == 'review-judgment':
            result = request(args.url, '/api/review', {'judgment_id': args.id})
        else:
            raise ValueError('未知操作。')
        emit(result, args.output)
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        emit({'error': str(error)})
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
